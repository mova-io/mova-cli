"""Sprint P — `mdk fmt` tests.

Three layers:

1. **Format detection** — :func:`detect_format` correctly classifies
   files by name + extension; unknown files return None.
2. **Pure formatters** — :func:`format_yaml`, :func:`format_prompt`,
   :func:`format_jsonl` are idempotent and handle edge cases
   (empty file, malformed input, unicode).
3. **CLI** — ``mdk fmt`` walks paths, ``--check`` exits non-zero on
   drift, ``--diff`` doesn't write, and skip-dirs are honored.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.fmt import (
    AGENT_YAML_KEY_ORDER,
    FormatError,
    detect_format,
    format_file,
    format_jsonl,
    format_prompt,
    format_yaml,
)
from movate.fmt.formatter import Format

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectFormat:
    def test_agent_yaml(self) -> None:
        assert detect_format(Path("agents/foo/agent.yaml")) is Format.AGENT_YAML

    def test_movate_yaml(self) -> None:
        assert detect_format(Path("movate.yaml")) is Format.MOVATE_YAML

    def test_mdk_yaml_alias(self) -> None:
        """Old mdk.yaml convention still recognized."""
        assert detect_format(Path("mdk.yaml")) is Format.MOVATE_YAML

    def test_policy_yaml(self) -> None:
        assert detect_format(Path("policy.yaml")) is Format.POLICY_YAML

    def test_generic_yaml(self) -> None:
        assert detect_format(Path("workflows/foo/workflow.yaml")) is Format.GENERIC_YAML

    def test_yml_extension(self) -> None:
        assert detect_format(Path("config.yml")) is Format.GENERIC_YAML

    def test_prompt_md_at_known_location(self) -> None:
        assert detect_format(Path("agents/foo/prompt.md")) is Format.PROMPT

    def test_jsonl(self) -> None:
        assert detect_format(Path("evals/foo/dataset.jsonl")) is Format.JSONL

    def test_context_md_detected_as_prompt(self) -> None:
        assert detect_format(Path("contexts/style.md")) is Format.PROMPT
        assert detect_format(Path("agents/rag-qa/contexts/rubric.md")) is Format.PROMPT

    def test_kb_json_detected_as_jsonl(self) -> None:
        assert detect_format(Path("kb/kb-lookup-corpus.json")) is Format.JSONL
        assert detect_format(Path("agents/foo/kb/corpus.json")) is Format.JSONL

    def test_unknown_extension(self) -> None:
        assert detect_format(Path("README.md")) is None
        assert detect_format(Path("script.py")) is None


# ---------------------------------------------------------------------------
# YAML formatter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatYaml:
    def test_reorders_keys_to_canonical_order(self) -> None:
        text = "prompt: Hi\nname: foo\napi_version: movate/v1\n"
        result = format_yaml(text, key_order=AGENT_YAML_KEY_ORDER)
        # api_version comes first, then name, then prompt
        lines = result.splitlines()
        api_idx = next(i for i, line in enumerate(lines) if line.startswith("api_version:"))
        name_idx = next(i for i, line in enumerate(lines) if line.startswith("name:"))
        prompt_idx = next(i for i, line in enumerate(lines) if line.startswith("prompt:"))
        assert api_idx < name_idx < prompt_idx

    def test_unknown_keys_preserve_relative_order(self) -> None:
        text = "zzz: 1\naaa: 2\nname: foo\n"
        result = format_yaml(text, key_order=AGENT_YAML_KEY_ORDER)
        # name comes first (canonical); then zzz and aaa preserve order
        lines = [line for line in result.splitlines() if line]
        assert lines[0].startswith("name:")
        # Among the unknown keys, zzz appeared first in input
        unknown = [line for line in lines if line.startswith(("zzz:", "aaa:"))]
        assert unknown[0].startswith("zzz:")
        assert unknown[1].startswith("aaa:")

    def test_idempotent(self) -> None:
        """Running format twice = running it once."""
        text = "prompt: Hi\nname: foo\napi_version: movate/v1\n"
        first = format_yaml(text, key_order=AGENT_YAML_KEY_ORDER)
        second = format_yaml(first, key_order=AGENT_YAML_KEY_ORDER)
        assert first == second

    def test_empty_yaml_returns_empty_string(self) -> None:
        assert format_yaml("") == ""
        assert format_yaml("# just a comment\n") == ""

    def test_invalid_yaml_raises(self) -> None:
        with pytest.raises(FormatError):
            format_yaml("not: : valid: :")

    def test_preserves_unicode(self) -> None:
        text = "name: 日本語\n"
        result = format_yaml(text)
        assert "日本語" in result


# ---------------------------------------------------------------------------
# Prompt (Markdown) formatter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatPrompt:
    def test_strips_trailing_whitespace_per_line(self) -> None:
        text = "Hello   \nWorld\t\n"
        assert format_prompt(text) == "Hello\nWorld\n"

    def test_collapses_multiple_blank_lines_to_one(self) -> None:
        text = "Hi\n\n\n\nThere\n"
        assert format_prompt(text) == "Hi\n\nThere\n"

    def test_ensures_single_trailing_newline(self) -> None:
        assert format_prompt("Hi") == "Hi\n"
        assert format_prompt("Hi\n") == "Hi\n"
        assert format_prompt("Hi\n\n\n") == "Hi\n"

    def test_preserves_leading_indentation(self) -> None:
        """Indented code blocks etc. must survive untouched."""
        text = "Example:\n\n    code line\n    more code\n"
        result = format_prompt(text)
        assert "    code line" in result
        assert "    more code" in result

    def test_empty_input_returns_empty_string(self) -> None:
        assert format_prompt("") == ""
        assert format_prompt("\n\n\n") == ""

    def test_idempotent(self) -> None:
        text = "Hi   \n\n\nThere\n\n\n"
        first = format_prompt(text)
        second = format_prompt(first)
        assert first == second


# ---------------------------------------------------------------------------
# JSONL formatter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatJsonl:
    def test_drops_blank_lines(self) -> None:
        text = '{"a": 1}\n\n{"b": 2}\n\n'
        result = format_jsonl(text)
        lines = result.strip().split("\n")
        assert len(lines) == 2

    def test_invalid_json_raises_with_line_number(self) -> None:
        text = '{"ok": true}\nNOT JSON\n'
        with pytest.raises(FormatError, match="line 2"):
            format_jsonl(text)

    def test_normalizes_whitespace_inside_record(self) -> None:
        text = '{"a":   1,"b":  2}\n'
        result = format_jsonl(text)
        # No double-spaces
        assert "   " not in result

    def test_preserves_record_key_order(self) -> None:
        """Eval datasets have implicit ordering — don't sort."""
        text = '{"input": "q", "expected_output": "a"}\n'
        result = format_jsonl(text)
        # input appears before expected_output
        assert result.index("input") < result.index("expected_output")

    def test_preserves_unicode(self) -> None:
        text = '{"name": "日本語"}\n'
        result = format_jsonl(text)
        assert "日本語" in result

    def test_empty_input_returns_empty_string(self) -> None:
        assert format_jsonl("") == ""
        assert format_jsonl("\n\n\n") == ""

    def test_idempotent(self) -> None:
        text = '{"a":1}\n{"b":2}\n\n'
        first = format_jsonl(text)
        second = format_jsonl(first)
        assert first == second


# ---------------------------------------------------------------------------
# format_file convenience
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_file_writes_changes_atomically(tmp_path: Path) -> None:
    path = tmp_path / "agents" / "foo" / "agent.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("prompt: Hi\nname: foo\napi_version: movate/v1\n")
    result = format_file(path)
    assert result.changed is True
    # File now has reordered keys
    reloaded = path.read_text()
    assert reloaded.index("api_version") < reloaded.index("name")


@pytest.mark.unit
def test_format_file_write_false_does_not_modify(tmp_path: Path) -> None:
    path = tmp_path / "agents" / "foo" / "agent.yaml"
    path.parent.mkdir(parents=True)
    original = "prompt: Hi\nname: foo\napi_version: movate/v1\n"
    path.write_text(original)
    result = format_file(path, write=False)
    assert result.changed is True
    # File is untouched
    assert path.read_text() == original


# ---------------------------------------------------------------------------
# CLI: mdk fmt
# ---------------------------------------------------------------------------


@pytest.fixture
def messy_project(tmp_path: Path) -> Path:
    """Project with un-formatted YAML and prompt files."""
    proj = tmp_path / "proj"
    proj.mkdir()

    # movate.yaml — keys out of order
    (proj / "movate.yaml").write_text("name: my-proj\napi_version: movate/v1\n")

    # An agent with messy YAML + prompt
    agent_dir = proj / "agents" / "triage"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "prompt: see prompt.md\nname: triage\napi_version: movate/v1\n"
    )
    (agent_dir / "prompt.md").write_text("Hello   \n\n\n\nWorld\n\n\n")

    # JSONL dataset
    evals = proj / "evals" / "triage"
    evals.mkdir(parents=True)
    (evals / "dataset.jsonl").write_text('{"a":1}\n\n{"b":2}\n\n')

    return proj


@pytest.mark.unit
def test_cli_fmt_writes_changes_by_default(messy_project: Path) -> None:
    result = runner.invoke(app, ["fmt", str(messy_project)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Re-running should now be clean
    second = runner.invoke(app, ["fmt", "--check", str(messy_project)])
    assert second.exit_code == 0, second.stdout + second.stderr


@pytest.mark.unit
def test_cli_fmt_check_exits_1_on_drift(messy_project: Path) -> None:
    result = runner.invoke(app, ["fmt", "--check", str(messy_project)])
    assert result.exit_code == 1
    assert "would be reformatted" in result.stdout.lower()


@pytest.mark.unit
def test_cli_fmt_check_does_not_write(messy_project: Path) -> None:
    original = (messy_project / "movate.yaml").read_text()
    runner.invoke(app, ["fmt", "--check", str(messy_project)])
    # File untouched
    assert (messy_project / "movate.yaml").read_text() == original


@pytest.mark.unit
def test_cli_fmt_diff_shows_diff_does_not_write(messy_project: Path) -> None:
    original = (messy_project / "movate.yaml").read_text()
    result = runner.invoke(app, ["fmt", "--diff", str(messy_project)])
    assert result.exit_code == 0
    # File untouched
    assert (messy_project / "movate.yaml").read_text() == original


@pytest.mark.unit
def test_cli_fmt_check_and_diff_mutually_exclusive(messy_project: Path) -> None:
    result = runner.invoke(app, ["fmt", "--check", "--diff", str(messy_project)])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_fmt_skips_junk_dirs(messy_project: Path) -> None:
    """Files under .venv, __pycache__ etc. must NOT be formatted."""
    junk = messy_project / ".venv" / "agent.yaml"
    junk.parent.mkdir(parents=True)
    junk.write_text("messy:1\n")
    runner.invoke(app, ["fmt", str(messy_project)])
    # Junk file is untouched
    assert junk.read_text() == "messy:1\n"


@pytest.mark.unit
def test_cli_fmt_no_formattable_files_prints_hint(tmp_path: Path) -> None:
    """An empty directory shouldn't crash — just inform."""
    result = runner.invoke(app, ["fmt", str(tmp_path)])
    assert result.exit_code == 0
    assert "no formattable files" in result.stdout.lower()


@pytest.mark.unit
def test_cli_fmt_single_file(messy_project: Path) -> None:
    """Targeting a specific file works."""
    target = messy_project / "movate.yaml"
    original = target.read_text()
    result = runner.invoke(app, ["fmt", str(target)])
    assert result.exit_code == 0
    assert target.read_text() != original


@pytest.mark.unit
def test_cli_fmt_invalid_yaml_exits_2(tmp_path: Path) -> None:
    """A broken file is reported as an error, exits 2."""
    bad = tmp_path / "movate.yaml"
    bad.write_text("not: : valid: : yaml:")
    result = runner.invoke(app, ["fmt", str(tmp_path)])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "failed to parse" in combined.lower() or "invalid yaml" in combined.lower()
