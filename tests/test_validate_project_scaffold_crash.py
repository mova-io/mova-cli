"""Regression tests for the P1 demo-onboarding crash (fix/validate-crash-project-agent).

Bug: ``mdk validate`` on a freshly-scaffolded agent (``mdk demo new`` /
``mdk init --project``) crashed with an uncaught pydantic ``ValidationError``
traceback (exit 1). Root cause: the project file (``movate.yaml`` /
``project.yaml``) that the scaffold writes carries a descriptive header
(``api_version`` / ``kind`` / ``name`` / ``version`` / ``description`` /
``storage`` + ``defaults.model.provider``). ``load_project_config`` validates
that file against :class:`ProjectConfig`, which is ``extra="forbid"`` and had
no fields for those keys — so every key raised ``extra_forbidden`` and the
loader (called from ``mdk validate``) propagated the raw error as a stack trace.

Fix, in two parts:

1. **Scope** — :class:`ProjectConfig` now declares the scaffold's
   project-identity / ``storage`` keys as accepted-but-unused metadata, and
   :class:`ModelParamDefaults` accepts the scaffold's default ``provider``.
   ``extra="forbid"`` is preserved everywhere, so a genuine typo still fails.
   The Agent schema (:class:`AgentSpec`) is untouched — project-only keys never
   reach it (only ``defaults:`` does, via ``layered_defaults``).
2. **Harden** — ``mdk validate`` now turns any project-config
   ``ValidationError`` into a clean, exit-2 diagnostic instead of a traceback.

Coverage:

* ProjectConfig accepts the full demo-new scaffold header (no raise).
* ``defaults`` still merges normally; identity keys don't bleed into agents.
* A genuine typo in a project-config block still fails (``extra=forbid`` intact).
* End-to-end: a project-scaffolded agent validates cleanly (exit 0, no traceback).
* End-to-end: a truly-invalid agent still fails with a clean message + exit 2.
* End-to-end: a typo'd project config yields a clean error + exit 2, no traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.config import ProjectConfig, load_project_config
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# The exact project file `mdk demo new` writes (kept in sync with
# movate.cli.demo_cmd._MOVATE_YAML). This is the shape that crashed.
_SCAFFOLD_MOVATE_YAML = """\
api_version: movate/v1
kind: Project
name: demo-faq
description: One-command runnable demo — an FAQ agent with sample eval dataset.
version: 0.1.0

defaults:
  model:
    provider: openai/gpt-4o-mini-2024-07-18
    params:
      temperature: 0.0
      max_tokens: 512

storage:
  backend: sqlite
  path: .movate/local.db
"""


# ---------------------------------------------------------------------------
# Unit: ProjectConfig schema scoping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_config_accepts_scaffold_header() -> None:
    """The full demo-new scaffold header loads without raising."""
    data = yaml.safe_load(_SCAFFOLD_MOVATE_YAML)
    cfg = ProjectConfig.model_validate(data)  # must not raise
    # Identity keys are captured, not dropped.
    assert cfg.name == "demo-faq"
    assert cfg.version == "0.1.0"
    assert cfg.api_version == "movate/v1"
    assert cfg.kind == "Project"
    assert cfg.storage is not None
    assert cfg.storage.backend == "sqlite"


@pytest.mark.unit
def test_scaffold_defaults_still_merge_and_identity_is_isolated() -> None:
    """The ``defaults:`` block still parses, and the project-identity keys
    are pure metadata — the merge surface (``defaults.model.params``) is
    unaffected by them."""
    cfg = ProjectConfig.model_validate(yaml.safe_load(_SCAFFOLD_MOVATE_YAML))
    # defaults.model.params still reaches the layered-defaults merge.
    assert cfg.defaults.model.params == {"temperature": 0.0, "max_tokens": 512}
    # The scaffold's default provider is accepted but is metadata-only
    # (layered_defaults never merges model.provider into an agent).
    assert cfg.defaults.model.provider == "openai/gpt-4o-mini-2024-07-18"


@pytest.mark.unit
def test_project_config_still_rejects_genuine_typo() -> None:
    """``extra="forbid"`` is preserved — a misspelled config block still
    fails loudly so a typo can't silently disable a policy."""
    bad = yaml.safe_load(_SCAFFOLD_MOVATE_YAML)
    bad["polcy"] = {"max_cost_per_run_usd": 0.5}  # typo for `policy`
    with pytest.raises(ValidationError) as ei:
        ProjectConfig.model_validate(bad)
    assert any(e["type"] == "extra_forbidden" for e in ei.value.errors())


@pytest.mark.unit
def test_load_project_config_reads_scaffold_movate_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the file loader: a scaffold-shaped movate.yaml at
    the project root loads cleanly (this is the exact disk path the crash
    took)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "movate.yaml").write_text(_SCAFFOLD_MOVATE_YAML)
    cfg = load_project_config()  # must not raise
    assert cfg.name == "demo-faq"


# ---------------------------------------------------------------------------
# End-to-end: `mdk validate` on a project-scaffolded agent
# ---------------------------------------------------------------------------


def _scaffold_project_with_agent(tmp_path: Path, *, project_file: str) -> Path:
    """Create ``tmp_path/agents/faq`` from the default template under a
    project root whose project file carries the scaffold header. Returns the
    agent dir. Mirrors the on-disk layout ``mdk demo new`` produces."""
    project_root = tmp_path
    agents_dir = project_root / "agents"
    agents_dir.mkdir()
    agent_dir = scaffold_agent(agents_dir / "faq", name="faq")
    # scaffold_agent writes a minimal movate.yaml at agents_dir (its parent);
    # overwrite the real project-root file with the scaffold header so the
    # loader's walk-up resolves the demo-shaped config.
    (project_root / project_file).write_text(_SCAFFOLD_MOVATE_YAML)
    # Remove the helper's stub so only our project file is the marker.
    stub = agents_dir / "movate.yaml"
    if stub.is_file():
        stub.unlink()
    return agent_dir


@pytest.mark.unit
def test_validate_project_scaffolded_agent_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repro: an agent scaffolded under a project whose movate.yaml has
    the demo header validates cleanly — exit 0, no traceback. Pre-fix this
    raised an uncaught pydantic ValidationError (exit 1)."""
    agent_dir = _scaffold_project_with_agent(tmp_path, project_file="movate.yaml")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Never a traceback in the output.
    assert "Traceback" not in result.stdout
    assert "extra_forbidden" not in result.stdout


@pytest.mark.unit
def test_validate_project_scaffolded_agent_project_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above but with the canonical ``project.yaml`` filename."""
    agent_dir = _scaffold_project_with_agent(tmp_path, project_file="project.yaml")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stdout


@pytest.mark.unit
def test_validate_invalid_agent_still_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truly-invalid agent.yaml (unknown top-level key) still fails with a
    clean, self-teaching message and exit 2 — the Agent schema stays
    ``extra=forbid``; we did not loosen it to mask typos."""
    agent_dir = _scaffold_project_with_agent(tmp_path, project_file="movate.yaml")
    yaml_path = agent_dir / "agent.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec["definitely_not_a_real_field"] = "oops"
    yaml_path.write_text(yaml.safe_dump(spec))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "Traceback" not in result.stdout
    assert "validation failed" in result.stdout
    assert "definitely_not_a_real_field" in result.stdout


@pytest.mark.unit
def test_validate_typod_project_config_is_clean_error_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine typo in the PROJECT config (not the agent) surfaces as a
    clean exit-2 diagnostic rather than an uncaught traceback — the
    hardened validate entrypoint (rule 10)."""
    agent_dir = _scaffold_project_with_agent(tmp_path, project_file="movate.yaml")
    # Append a typo'd config block to the project file.
    project_file = tmp_path / "movate.yaml"
    project_file.write_text(project_file.read_text() + "\npolcy:\n  max_cost_per_run_usd: 0.5\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "Traceback" not in result.stdout
    assert "project config failed to load" in result.stdout
    assert "polcy" in result.stdout
