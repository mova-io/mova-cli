"""``movate validate`` checks the AgentRuntime field.

The runtime field (added in Tier-2 #5) lets agents declare
``runtime: native_anthropic`` / ``native_openai`` / ``langchain`` —
but those adapters don't ship until Tier-2 #6/#7/#8. Validate
should reject unwired runtimes at parse time so the operator
learns BEFORE they try to run, not after.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.models import AgentRuntime
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


def _set_runtime(agent_dir: Path, runtime: AgentRuntime) -> None:
    yaml_path = agent_dir / "agent.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec["runtime"] = runtime.value
    yaml_path.write_text(yaml.safe_dump(spec))


@pytest.mark.unit
def test_validate_accepts_default_litellm_runtime(tmp_path: Path) -> None:
    """No ``runtime:`` field → defaults to litellm → validate passes
    + shows ``runtime: litellm`` in the success banner."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "runtime:     litellm" in result.stdout


@pytest.mark.unit
def test_validate_accepts_explicit_litellm_runtime(tmp_path: Path) -> None:
    """Explicit ``runtime: litellm`` also passes."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.LITELLM)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "runtime",
    # Only the still-unwired runtimes belong here. NATIVE_ANTHROPIC was
    # added in Tier-2 #6 — when this build has the ``anthropic`` extra
    # installed (the default dev env does), declaring it should now
    # PASS validate, not fail. See test_validate_accepts_native_anthropic_when_installed.
    [AgentRuntime.NATIVE_OPENAI, AgentRuntime.LANGCHAIN],
)
def test_validate_rejects_unwired_runtime(tmp_path: Path, runtime: AgentRuntime) -> None:
    """Native_openai + LangChain don't ship adapters yet (Tier-2 #7 / #8).
    Declaring them in agent.yaml should fail validate with a clear
    message naming the unwired runtime and listing what IS available."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, runtime)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "unsupported runtime" in result.stdout
    assert runtime.value in result.stdout
    # The "what IS available" line names litellm at minimum.
    assert "litellm" in result.stdout


@pytest.mark.unit
def test_validate_accepts_native_anthropic_when_installed(tmp_path: Path) -> None:
    """Once the ``anthropic`` extra is installed (Tier-2 #6 landed),
    ``runtime: native_anthropic`` should pass validate — same code
    path as the LiteLLM happy case."""
    pytest.importorskip("anthropic")
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.NATIVE_ANTHROPIC)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "runtime:     native_anthropic" in result.stdout


@pytest.mark.unit
def test_validate_rejects_unknown_runtime_string(tmp_path: Path) -> None:
    """A string that isn't even a known AgentRuntime value fails
    at YAML load time (Pydantic enum validation) — exit 2 with a
    load-error message, not the runtime-availability message."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    yaml_path = agent_dir / "agent.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec["runtime"] = "telepathy"
    yaml_path.write_text(yaml.safe_dump(spec))

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2
    assert "validation failed" in result.stdout
