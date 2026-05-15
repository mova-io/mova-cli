"""Sprint Q — `mdk tune` tests.

Three layers:

1. **Parsing** — _parse_sweep accepts known keys, rejects unknowns,
   handles bad format / empty values; _coerce_sweep_value picks
   the right type per key.
2. **Bundle override** — _override_bundle produces a new AgentBundle
   with the model block mutated, leaving the original unchanged.
3. **CLI** — `mdk tune <agent> <input> --sweep ...` end-to-end with
   MockProvider, --runs > 1, --json output, error paths.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import click
import pytest
from typer import BadParameter
from typer.testing import CliRunner

from movate.cli.main import app
from movate.cli.tune_cmd import (
    _coerce_sweep_value,
    _override_bundle,
    _parse_sweep,
    _truncate,
)
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
    """Project with one agent + isolated MOVATE_DB so tune doesn't write
    to the operator's real local.db."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\nkind: Project\nname: t\n")
    _scaffold_agent(tmp_path / "agents" / "demo", name="demo")
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "test.db"))
    return tmp_path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseSweep:
    def test_temperature(self) -> None:
        key, values = _parse_sweep("temperature=0.0,0.5,1.0")
        assert key == "temperature"
        assert values == [0.0, 0.5, 1.0]

    def test_max_tokens(self) -> None:
        key, values = _parse_sweep("max_tokens=128,512,1024")
        assert key == "max_tokens"
        assert values == [128, 512, 1024]

    def test_model(self) -> None:
        key, values = _parse_sweep(
            "model=openai/gpt-4o-mini,anthropic/claude-haiku-4-5-20251001"
        )
        assert key == "model"
        assert values == ["openai/gpt-4o-mini", "anthropic/claude-haiku-4-5-20251001"]

    def test_unknown_key_exits_2(self) -> None:
        with pytest.raises(click.exceptions.Exit) as exc:
            _parse_sweep("bogus=1,2,3")
        assert exc.value.exit_code == 2

    def test_no_equals_exits_2(self) -> None:
        with pytest.raises(click.exceptions.Exit) as exc:
            _parse_sweep("temperature")
        assert exc.value.exit_code == 2

    def test_empty_values_exits_2(self) -> None:
        with pytest.raises(click.exceptions.Exit) as exc:
            _parse_sweep("temperature=")
        assert exc.value.exit_code == 2


@pytest.mark.unit
class TestCoerceSweepValue:
    def test_temperature_to_float(self) -> None:
        assert _coerce_sweep_value("temperature", "0.7") == 0.7

    def test_max_tokens_to_int(self) -> None:
        assert _coerce_sweep_value("max_tokens", "512") == 512

    def test_model_stays_string(self) -> None:
        assert _coerce_sweep_value("model", "openai/x") == "openai/x"

    def test_bad_temperature_raises(self) -> None:
        with pytest.raises(BadParameter):
            _coerce_sweep_value("temperature", "not-a-number")

    def test_bad_max_tokens_raises(self) -> None:
        with pytest.raises(BadParameter):
            _coerce_sweep_value("max_tokens", "1.5")


# ---------------------------------------------------------------------------
# Bundle override
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_override_bundle_changes_temperature(project: Path) -> None:
    bundle = load_agent(project / "agents" / "demo")
    new_bundle = _override_bundle(bundle, "temperature", 0.7)
    # New bundle's model.params has the override
    assert new_bundle.spec.model.params.get("temperature") == 0.7
    # Original bundle untouched (it wasn't constructed with temperature
    # in params, so it stays absent or at its original value).
    assert bundle.spec.model.params.get("temperature") != 0.7


@pytest.mark.unit
def test_override_bundle_changes_max_tokens(project: Path) -> None:
    bundle = load_agent(project / "agents" / "demo")
    new_bundle = _override_bundle(bundle, "max_tokens", 256)
    assert new_bundle.spec.model.params.get("max_tokens") == 256


@pytest.mark.unit
def test_override_bundle_changes_model_provider(project: Path) -> None:
    bundle = load_agent(project / "agents" / "demo")
    original_provider = bundle.spec.model.provider
    new_bundle = _override_bundle(bundle, "model", "anthropic/claude-haiku-4-5-20251001")
    assert new_bundle.spec.model.provider == "anthropic/claude-haiku-4-5-20251001"
    # Original unchanged
    assert bundle.spec.model.provider == original_provider


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_truncate_short_string_unchanged() -> None:
    assert _truncate("hello") == "hello"


@pytest.mark.unit
def test_truncate_long_string_gets_ellipsis() -> None:
    long = "x" * 200
    result = _truncate(long, limit=50)
    assert len(result) == 50
    assert result.endswith("…")


@pytest.mark.unit
def test_truncate_collapses_newlines() -> None:
    assert "\n" not in _truncate("line1\nline2")


# ---------------------------------------------------------------------------
# CLI: happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_tune_temperature_sweep(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "tune",
            "demo",
            '{"text": "hello"}',
            "--sweep",
            "temperature=0.0,0.5",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Both swept values appear in the table
    assert "0.0" in result.stdout
    assert "0.5" in result.stdout


@pytest.mark.unit
def test_cli_tune_runs_multiple_samples(project: Path) -> None:
    """--runs 3 should produce 3 samples per swept value (verified via --json)."""
    result = runner.invoke(
        app,
        [
            "tune",
            "demo",
            '{"text": "hello"}',
            "--sweep",
            "temperature=0.0,0.5",
            "--runs",
            "3",
            "--mock",
            "--json",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    # 2 values * 3 samples = 6 entries
    assert len(data) == 6


@pytest.mark.unit
def test_cli_tune_model_sweep(project: Path) -> None:
    """Sweep across providers — under --mock all should succeed."""
    result = runner.invoke(
        app,
        [
            "tune",
            "demo",
            '{"text": "hello"}',
            "--sweep",
            "model=openai/gpt-4o-mini-2024-07-18,anthropic/claude-haiku-4-5-20251001",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "openai" in result.stdout
    assert "anthropic" in result.stdout


# ---------------------------------------------------------------------------
# CLI: error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_tune_unknown_agent_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "tune",
            "ghost",
            '{"text": "x"}',
            "--sweep",
            "temperature=0.0",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_tune_bad_sweep_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "tune",
            "demo",
            '{"text": "x"}',
            "--sweep",
            "bogus=1",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_tune_zero_runs_exits_2(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "tune",
            "demo",
            '{"text": "x"}',
            "--sweep",
            "temperature=0.0",
            "--runs",
            "0",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_tune_non_object_input_exits_2(project: Path) -> None:
    """A bare string isn't a JSON object — must be rejected."""
    result = runner.invoke(
        app,
        [
            "tune",
            "demo",
            '"just a string"',
            "--sweep",
            "temperature=0.0",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    # Typer.BadParameter exits 2.
    assert result.exit_code == 2
