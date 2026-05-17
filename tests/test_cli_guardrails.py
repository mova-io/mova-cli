"""``mdk guardrails {test, list, enable, disable}`` — CLI wrapper around the J-0 engine.

Coverage:

* **test** — exits 0 on allow/redact/warn, exits 1 on block;
  renders matched terms; supports input + output directions;
  warns when guardrails are permissive.
* **list** — renders the configured guardrails per direction + module;
  shows enabled flag, mode, detail counts; supports --json.
* **enable / disable** — minimal-diff write to movate.yaml; validates
  the resulting config; rejects invalid <direction>.<module> paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _write_config(home: Path, *, guardrails: dict) -> Path:
    """Drop a movate.yaml with the given guardrails block at ``home``."""
    cfg_path = home / "movate.yaml"
    cfg_path.write_text(yaml.safe_dump({"guardrails": guardrails}, sort_keys=False))
    return cfg_path


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each CLI invocation from a temp project root so the
    ``load_project_config`` call reads our fixture YAML, not the
    repo's real movate.yaml."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_warns_when_no_guardrails_enabled(project_root: Path) -> None:
    """No config + no guardrails enabled → friendly hint, exit 0."""
    _write_config(project_root, guardrails={})
    result = runner.invoke(app, ["guardrails", "test", "any text"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no guardrails" in result.stdout.lower()


@pytest.mark.unit
def test_test_blocks_text_with_pii(project_root: Path) -> None:
    """PII present + mode=block → exit 1 + 'BLOCK' rendered."""
    _write_config(
        project_root,
        guardrails={"input": {"pii": {"enabled": True, "mode": "block"}}},
    )
    result = runner.invoke(
        app,
        ["guardrails", "test", "Reach me at jane@example.com"],
    )
    assert result.exit_code == 1
    assert "BLOCK" in result.stdout


@pytest.mark.unit
def test_test_redacts_pii_and_shows_replacement(project_root: Path) -> None:
    """PII + mode=redact → exit 0; the rendered output shows the
    redacted form so the operator can verify what the model would see."""
    _write_config(
        project_root,
        guardrails={"input": {"pii": {"enabled": True, "mode": "redact"}}},
    )
    result = runner.invoke(
        app,
        ["guardrails", "test", "Email jane@example.com please"],
    )
    assert result.exit_code == 0
    assert "REDACT" in result.stdout.upper()
    # The redacted output contains the marker, not the original email.
    assert "[REDACTED:email]" in result.stdout
    assert "jane@example.com" in result.stdout  # original shown for comparison


@pytest.mark.unit
def test_test_output_direction_uses_output_guardrails(project_root: Path) -> None:
    """--direction output reads the OUTPUT guardrails, not input."""
    _write_config(
        project_root,
        guardrails={
            "input": {},
            "output": {
                "content": {
                    "enabled": True,
                    "banned_terms": ["secret"],
                    "on_violation": "block",
                }
            },
        },
    )
    # input direction: permissive (no input guardrails enabled)
    result = runner.invoke(app, ["guardrails", "test", "this is secret", "--direction", "input"])
    assert result.exit_code == 0
    assert "no guardrails" in result.stdout.lower()
    # output direction: blocked
    result = runner.invoke(app, ["guardrails", "test", "this is secret", "--direction", "output"])
    assert result.exit_code == 1
    assert "BLOCK" in result.stdout


@pytest.mark.unit
def test_test_invalid_direction_exits_two(project_root: Path) -> None:
    _write_config(project_root, guardrails={})
    result = runner.invoke(app, ["guardrails", "test", "any", "--direction", "sideways"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_shows_every_direction_module_pair(project_root: Path) -> None:
    """list emits 6 rows: 2 directions x 3 modules. All show up even
    when disabled."""
    _write_config(project_root, guardrails={})
    result = runner.invoke(app, ["guardrails", "list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # All 6 paths render in the table.
    for path in (
        "input.pii",
        "input.topic",
        "input.content",
        "output.pii",
        "output.topic",
        "output.content",
    ):
        assert path in result.stdout


@pytest.mark.unit
def test_list_renders_enabled_modules_with_details(project_root: Path) -> None:
    _write_config(
        project_root,
        guardrails={
            "input": {
                "pii": {"enabled": True, "mode": "redact", "types": ["email", "phone"]},
                "content": {
                    "enabled": True,
                    "banned_terms": ["a", "b", "c"],
                    "on_violation": "block",
                },
            }
        },
    )
    result = runner.invoke(app, ["guardrails", "list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # PII mode shows redact; types appear.
    assert "redact" in result.stdout
    assert "email" in result.stdout
    # Content shows 3 banned terms.
    assert "3 banned" in result.stdout


@pytest.mark.unit
def test_list_json_emits_parseable(project_root: Path) -> None:
    _write_config(
        project_root,
        guardrails={"input": {"pii": {"enabled": True, "mode": "warn"}}},
    )
    result = runner.invoke(app, ["guardrails", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["input"]["pii"]["enabled"] is True
    assert payload["input"]["pii"]["mode"] == "warn"


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enable_flips_the_bit_in_yaml(project_root: Path) -> None:
    """`enable input.pii` writes ``enabled: true`` under
    guardrails.input.pii in movate.yaml."""
    cfg_path = _write_config(project_root, guardrails={})
    result = runner.invoke(app, ["guardrails", "enable", "input.pii"])
    assert result.exit_code == 0, result.stdout + result.stderr
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["guardrails"]["input"]["pii"]["enabled"] is True


@pytest.mark.unit
def test_disable_flips_the_bit_back(project_root: Path) -> None:
    cfg_path = _write_config(
        project_root,
        guardrails={"input": {"pii": {"enabled": True, "mode": "redact"}}},
    )
    result = runner.invoke(app, ["guardrails", "disable", "input.pii"])
    assert result.exit_code == 0, result.stdout + result.stderr
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["guardrails"]["input"]["pii"]["enabled"] is False
    # Surrounding fields (mode=redact) preserved — re-enable would restore.
    assert raw["guardrails"]["input"]["pii"]["mode"] == "redact"


@pytest.mark.unit
def test_enable_creates_yaml_when_missing(project_root: Path) -> None:
    """`enable` works against a fresh project with no movate.yaml yet."""
    cfg_path = project_root / "movate.yaml"
    assert not cfg_path.exists()
    result = runner.invoke(app, ["guardrails", "enable", "output.content"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert cfg_path.exists()
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["guardrails"]["output"]["content"]["enabled"] is True


@pytest.mark.unit
def test_enable_invalid_path_exits_two(project_root: Path) -> None:
    """Typo in the path → clean error with the valid set listed."""
    _write_config(project_root, guardrails={})
    result = runner.invoke(app, ["guardrails", "enable", "inpit.pii"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "invalid path" in combined.lower()
