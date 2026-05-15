"""Sprint R — `mdk eval-gen` tests.

Three layers:

1. **Mock input synthesis** — _mock_input_for_schema produces a valid
   input for each declared property type and is deterministic per seed.
2. **CLI happy path** — `mdk eval-gen --mock` writes a JSONL file
   with the right shape end-to-end against a real agent.
3. **Safety + validation** — bad --num, bad --sample-input, existing
   output file without --force, missing agent all surface clean errors.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from movate.cli.eval_gen_cmd import _mock_input_for_schema
from movate.cli.main import app

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
    """Project with one agent + isolated MOVATE_DB."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\nkind: Project\nname: t\n")
    _scaffold_agent(tmp_path / "agents" / "demo", name="demo")
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "test.db"))
    return tmp_path


# ---------------------------------------------------------------------------
# Mock input synthesis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMockInputForSchema:
    def test_string_field_produces_string(self) -> None:
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        result = _mock_input_for_schema(schema, seed=0)
        assert isinstance(result["q"], str)

    def test_integer_field_produces_int(self) -> None:
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
        result = _mock_input_for_schema(schema, seed=0)
        assert isinstance(result["n"], int)

    def test_boolean_field_produces_bool(self) -> None:
        schema = {
            "type": "object",
            "properties": {"flag": {"type": "boolean"}},
            "required": ["flag"],
        }
        result = _mock_input_for_schema(schema, seed=0)
        assert isinstance(result["flag"], bool)

    def test_result_satisfies_schema(self) -> None:
        """The whole point: the synthesized input must validate."""
        schema = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["text", "count"],
        }
        result = _mock_input_for_schema(schema, seed=42)
        # Doesn't raise → satisfies
        Draft202012Validator(schema).validate(result)

    def test_deterministic_per_seed(self) -> None:
        """Same seed → same output. Lets tests assert on full content."""
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        first = _mock_input_for_schema(schema, seed=7)
        second = _mock_input_for_schema(schema, seed=7)
        assert first == second

    def test_different_seeds_produce_different_outputs(self) -> None:
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        a = _mock_input_for_schema(schema, seed=1)
        b = _mock_input_for_schema(schema, seed=2)
        # Strings are randomized → distinct seeds shouldn't collide.
        assert a != b


# ---------------------------------------------------------------------------
# CLI: happy path (--mock so no real LLM call)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_eval_gen_writes_jsonl(project: Path) -> None:
    result = runner.invoke(
        app,
        ["eval-gen", "demo", "--num", "3", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    out = project / "evals" / "demo" / "dataset.generated.jsonl"
    assert out.is_file()
    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3
    # Each line is a JSON object with the expected shape
    for ln in lines:
        entry = json.loads(ln)
        assert "input" in entry
        assert "expected" in entry
        assert entry["generated"] is True


@pytest.mark.unit
def test_cli_eval_gen_custom_output(project: Path) -> None:
    out = project / "custom" / "set.jsonl"
    result = runner.invoke(
        app,
        [
            "eval-gen",
            "demo",
            "--num",
            "2",
            "--mock",
            "--output",
            str(out),
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    assert out.is_file()


@pytest.mark.unit
def test_cli_eval_gen_force_overwrites_existing(project: Path) -> None:
    out = project / "evals" / "demo" / "dataset.generated.jsonl"
    out.parent.mkdir(parents=True)
    out.write_text("old content\n")
    result = runner.invoke(
        app,
        [
            "eval-gen",
            "demo",
            "--num",
            "1",
            "--mock",
            "--force",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    # File was replaced
    text = out.read_text()
    assert "old content" not in text


# ---------------------------------------------------------------------------
# CLI: error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_eval_gen_zero_num_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        ["eval-gen", "demo", "--num", "0", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_eval_gen_num_above_cap_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "eval-gen",
            "demo",
            "--num",
            "10000",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_eval_gen_bad_sample_input_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "eval-gen",
            "demo",
            "--num",
            "1",
            "--mock",
            "--sample-input",
            "not-valid-json",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_eval_gen_non_object_sample_input_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "eval-gen",
            "demo",
            "--num",
            "1",
            "--mock",
            "--sample-input",
            '"just a string"',
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_eval_gen_refuses_existing_output_without_force(project: Path) -> None:
    out = project / "evals" / "demo" / "dataset.generated.jsonl"
    out.parent.mkdir(parents=True)
    out.write_text("keep me\n")
    result = runner.invoke(
        app,
        ["eval-gen", "demo", "--num", "1", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 2
    # Original preserved
    assert out.read_text() == "keep me\n"


@pytest.mark.unit
def test_cli_eval_gen_missing_agent_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        ["eval-gen", "ghost", "--num", "1", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 2
