"""Sprint R — `mdk simulate` tests.

Three layers:

1. **Helpers** — input-key picking, response flattening, marker
   detection, scenario JSON parsing, scenario file loading.
2. **Mock scenario cycle** — `_mock_scenarios` returns the requested
   count, cycling stock entries.
3. **CLI** — `mdk simulate --mock` runs end-to-end against a real
   agent and emits a summary table + optional JSON output.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer import BadParameter
from typer.testing import CliRunner

from movate.cli.main import app
from movate.cli.simulate_cmd import (
    _flatten_response,
    _is_give_up,
    _is_goal_achieved,
    _load_scenarios,
    _mock_scenarios,
    _parse_scenario_json,
    _pick_chat_input_key,
)

runner = CliRunner(mix_stderr=False)

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold_agent(dst: Path, name: str = "chatbot") -> Path:
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project with one chatbot agent + isolated MOVATE_DB."""
    _scaffold_agent(tmp_path / "agents" / "chatbot", name="chatbot")
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "test.db"))
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPickChatInputKey:
    def test_prefers_message(self) -> None:
        schema = {"properties": {"message": {"type": "string"}}, "required": ["message"]}
        assert _pick_chat_input_key(schema) == "message"

    def test_prefers_text_over_unknown(self) -> None:
        schema = {
            "properties": {
                "text": {"type": "string"},
                "weirdo": {"type": "string"},
            },
            "required": ["text", "weirdo"],
        }
        assert _pick_chat_input_key(schema) == "text"

    def test_falls_back_to_first_required_string(self) -> None:
        schema = {
            "properties": {"custom_field": {"type": "string"}},
            "required": ["custom_field"],
        }
        assert _pick_chat_input_key(schema) == "custom_field"

    def test_returns_message_when_nothing_matches(self) -> None:
        """Empty schema → fallback to 'message' (will validate-fail
        downstream, but at least we have a defined behavior)."""
        assert _pick_chat_input_key({}) == "message"


@pytest.mark.unit
class TestFlattenResponse:
    def test_string_passthrough(self) -> None:
        assert _flatten_response("hello") == "hello"

    def test_dict_prefers_reply_key(self) -> None:
        assert _flatten_response({"reply": "ok", "other": "x"}) == "ok"

    def test_dict_falls_back_to_response(self) -> None:
        assert _flatten_response({"response": "y"}) == "y"

    def test_dict_falls_back_to_json_dump(self) -> None:
        """Unknown keys → serialize the whole dict so the operator
        still sees something rather than 'undefined'."""
        out = _flatten_response({"weird": "shape", "n": 1})
        assert "weird" in out and "shape" in out


@pytest.mark.unit
class TestMarkers:
    def test_goal_marker_detected(self) -> None:
        assert _is_goal_achieved("yes thanks [GOAL_ACHIEVED]")
        assert not _is_goal_achieved("not yet")

    def test_give_up_marker_detected(self) -> None:
        assert _is_give_up("ugh [GIVE_UP]")
        assert not _is_give_up("still trying")


@pytest.mark.unit
class TestParseScenarioJson:
    def test_valid_json(self) -> None:
        raw = json.dumps(
            {
                "persona": "an angry user",
                "goal": "get refund",
                "initial_message": "where is my money",
            }
        )
        s = _parse_scenario_json(raw)
        assert s.persona == "an angry user"
        assert s.goal == "get refund"

    def test_strips_code_fences(self) -> None:
        raw = '```json\n{"persona": "x", "goal": "y", "initial_message": "z"}\n```'
        s = _parse_scenario_json(raw)
        assert s.persona == "x"
        assert s.initial_message == "z"

    def test_bad_json_yields_fallback(self) -> None:
        s = _parse_scenario_json("not json at all")
        # Returns a generic test user, not a crash
        assert s.persona  # non-empty
        assert s.initial_message  # non-empty

    def test_non_object_yields_fallback(self) -> None:
        s = _parse_scenario_json('"just a string"')
        assert s.persona  # fallback


@pytest.mark.unit
class TestLoadScenarios:
    def test_loads_jsonl_file(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {
                    "persona": "p1",
                    "goal": "g1",
                    "initial_message": "hi",
                    "max_turns": 4,
                }
            )
            + "\n"
            + json.dumps({"persona": "p2", "goal": "g2", "initial_message": "hey"})
            + "\n"
        )
        scenarios = _load_scenarios(path)
        assert len(scenarios) == 2
        assert scenarios[0].max_turns == 4
        # Default max_turns when omitted
        assert scenarios[1].max_turns > 0

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text('\n\n{"persona": "p", "goal": "g", "initial_message": "m"}\n\n')
        scenarios = _load_scenarios(path)
        assert len(scenarios) == 1

    def test_bad_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text("not valid json\n")
        with pytest.raises(BadParameter):
            _load_scenarios(path)

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text("")
        with pytest.raises(BadParameter):
            _load_scenarios(path)


# ---------------------------------------------------------------------------
# Mock scenarios
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMockScenarios:
    def test_returns_requested_count(self) -> None:
        scenarios = _mock_scenarios(3)
        assert len(scenarios) == 3

    def test_cycles_when_count_exceeds_stock(self) -> None:
        """N > stock size cycles through stock list."""
        scenarios = _mock_scenarios(10)
        assert len(scenarios) == 10
        # Each is a real Scenario
        for s in scenarios:
            assert s.persona
            assert s.goal


# ---------------------------------------------------------------------------
# CLI — happy path through --mock
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_simulate_mock_runs_scenarios(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "simulate",
            "chatbot",
            "--num",
            "2",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Simulation results" in result.stdout
    # Both scenarios appear with goal markers
    assert "achieved" in result.stdout.lower()


@pytest.mark.unit
def test_cli_simulate_output_writes_transcripts(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "results.json"
    result = runner.invoke(
        app,
        [
            "simulate",
            "chatbot",
            "--num",
            "1",
            "--mock",
            "--output",
            str(out),
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    assert out.is_file()
    data = json.loads(out.read_text())
    assert len(data) == 1
    # The transcript has at least the initial user message + 1 bot reply
    transcript = data[0]["transcript"]
    assert len(transcript) >= 2
    assert transcript[0]["role"] == "user"
    assert transcript[1]["role"] == "assistant"


@pytest.mark.unit
def test_cli_simulate_with_scenarios_file(project: Path, tmp_path: Path) -> None:
    s_path = tmp_path / "scenarios.jsonl"
    s_path.write_text(
        json.dumps({"persona": "custom user", "goal": "custom goal", "initial_message": "hi there"})
        + "\n"
    )
    result = runner.invoke(
        app,
        [
            "simulate",
            "chatbot",
            "--scenarios",
            str(s_path),
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    # Custom persona text appears in the summary
    assert "custom user" in result.stdout


# ---------------------------------------------------------------------------
# CLI — flag validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_simulate_zero_num_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        ["simulate", "chatbot", "--num", "0", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_simulate_num_above_cap_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "simulate",
            "chatbot",
            "--num",
            "10000",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_simulate_max_turns_above_cap_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "simulate",
            "chatbot",
            "--num",
            "1",
            "--max-turns",
            "999",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_simulate_missing_agent_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        ["simulate", "ghost", "--num", "1", "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 2
