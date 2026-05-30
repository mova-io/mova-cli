"""Tests for ``mdk skills`` — scaffold, list, run.

Three subcommands; each gets coverage on the happy path + the operator-
facing failure modes:

* ``list`` — empty registry hint, populated table with name + backend +
  cost columns, surfaces a SkillLoadError if a project skill.yaml is
  malformed.
* ``scaffold`` — produces the expected file tree with name substitution,
  refuses to clobber an existing dir without --force.
* ``run`` — happy path (Python skill returns dict), invalid JSON input
  rejected before any skill load, unknown skill name surfaces with a
  ``hint:`` suggesting scaffold, SkillError exits non-zero with the
  type tag visible.

Tests run the skill registry against tmp_path; the scaffold tests
construct a real Python skill on disk + add the parent to sys.path so
``mdk skills run`` can import the impl module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.skill_loader import load_skill

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_python_skill(
    parent: Path,
    name: str,
    *,
    impl_body: str = "    return {'result': 'echo:' + str(input)}",
) -> Path:
    """Drop a python-backed skill at <parent>/skills/<name>/.

    The impl is wired so ``<name>.impl:run`` resolves via importlib —
    the test harness adds the parent to sys.path before invoking.
    """
    skill_dir = parent / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "__init__.py").write_text("")
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {query: string}\n"
        "  output: {result: string}\n"
        "implementation:\n"
        "  kind: python\n"
        f"  entry: {name}.impl:run\n"
    )
    (skill_dir / "impl.py").write_text("def run(input, ctx):\n" + impl_body + "\n")
    return skill_dir


# ---------------------------------------------------------------------------
# `mdk skills list`
# ---------------------------------------------------------------------------


def test_list_empty_registry_shows_hint(tmp_path: Path) -> None:
    """No ``skills/`` folder → friendly message with a `scaffold` hint
    instead of a blank table or a hard error."""
    result = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "no skills registered" in result.stdout
    assert "scaffold" in result.stdout


def test_list_populated_registry_renders_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two python skills, both should appear in the table with name +
    backend + entry columns visible."""
    _write_python_skill(tmp_path, "alpha")
    _write_python_skill(tmp_path, "beta")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # Strip ANSI for tolerant matching across terminal widths.
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "alpha" in cleaned
    assert "beta" in cleaned
    assert "python" in cleaned


def test_list_malformed_skill_yaml_errors(tmp_path: Path) -> None:
    """One broken skill.yaml in the registry → clean error, not crash."""
    skill_dir = tmp_path / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text("not: valid: yaml: at all:")
    result = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "registry load failed" in combined or "skill" in combined.lower()


# ---------------------------------------------------------------------------
# `mdk skills scaffold`
# ---------------------------------------------------------------------------


def test_scaffold_creates_expected_file_tree(tmp_path: Path) -> None:
    """The scaffold should produce a working skill folder — yaml + impl
    + README — with the name substituted into each file."""
    result = runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    skill_dir = tmp_path / "skills" / "weather"
    assert (skill_dir / "skill.yaml").exists()
    assert (skill_dir / "impl.py").exists()
    assert (skill_dir / "README.md").exists()
    # Name substitution worked in every file.
    yaml_body = (skill_dir / "skill.yaml").read_text()
    assert "name: weather" in yaml_body
    assert "weather.impl:run" in yaml_body
    readme = (skill_dir / "README.md").read_text()
    assert "weather" in readme


def test_scaffold_refuses_overwrite_without_force(tmp_path: Path) -> None:
    result = runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    assert result.exit_code == 0
    # Second invocation without --force must refuse.
    second = runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    assert second.exit_code == 2
    combined = second.stdout + (second.stderr or "")
    assert "already exists" in combined


def test_scaffold_force_overwrites(tmp_path: Path) -> None:
    """First scaffold then mutate the impl; second scaffold with --force
    restores the template body."""
    runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    impl_path = tmp_path / "skills" / "weather" / "impl.py"
    impl_path.write_text("# vandalized\n")
    result = runner.invoke(
        app, ["skills", "scaffold", "weather", "--project", str(tmp_path), "--force"]
    )
    assert result.exit_code == 0
    # Template body restored.
    assert "vandalized" not in impl_path.read_text()
    assert "echo:" in impl_path.read_text()


def test_scaffolded_skill_loads_in_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: scaffold a skill, then list it — proves the
    generated skill.yaml validates cleanly through ``mdk validate``-
    style parsing."""
    runner.invoke(app, ["skills", "scaffold", "demo", "--project", str(tmp_path)])
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    listed = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert listed.exit_code == 0
    assert "demo" in listed.stdout


# ---------------------------------------------------------------------------
# `mdk skills run`
# ---------------------------------------------------------------------------


def test_run_happy_path_emits_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a python skill end-to-end via the CLI; stdout has the
    pretty-printed JSON result, stderr has the ✓ banner so a pipe
    captures only the payload."""
    _write_python_skill(tmp_path, "echo", impl_body="    return {'result': input['query']}")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "echo",
            '{"query": "hello"}',
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # stdout = pretty JSON payload
    out = json.loads(result.stdout)
    assert out == {"result": "hello"}
    # stderr = success banner (so pipes get clean JSON on stdout)
    assert "echo" in (result.stderr or "")


def test_run_rejects_invalid_json_input(tmp_path: Path) -> None:
    """Bad JSON input is caught BEFORE any skill load — fast feedback."""
    _write_python_skill(tmp_path, "echo")
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "echo",
            "not json {",
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "JSON" in combined or "not valid" in combined.lower()


def test_run_rejects_non_object_input(tmp_path: Path) -> None:
    """JSON list / scalar at top-level is rejected — skill inputs must
    be objects."""
    _write_python_skill(tmp_path, "echo")
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "echo",
            '["not", "an", "object"]',
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2


def test_run_unknown_skill_hints_scaffold(tmp_path: Path) -> None:
    """Operator typo'd the skill name. We point them at `skills list`
    + `skills scaffold` rather than a bare "not found"."""
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "nonexistent",
            "{}",
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "scaffold" in combined or "list" in combined.lower()


def test_run_skill_error_surfaces_type_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill that raises → exit 1 (distinct from exit 2 for the
    CLI's own input errors) + the SkillErrorType visible in stderr."""
    _write_python_skill(
        tmp_path,
        "exploder",
        impl_body="    raise RuntimeError('boom')",
    )
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "exploder",
            '{"query": "x"}',
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "backend_error" in combined
    assert "boom" in combined


# ---------------------------------------------------------------------------
# `mdk skills search`
# ---------------------------------------------------------------------------


def _write_tagged_skill(
    parent: Path,
    name: str,
    tags: list[str],
    description: str = "",
    kind: str = "python",
) -> Path:
    """Minimal skill yaml with tags for search tests."""
    skill_dir = parent / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "__init__.py").write_text("")
    tags_yaml = "\n".join(f"  - {t}" for t in tags) if tags else ""
    tags_block = f"tags:\n{tags_yaml}\n" if tags else ""
    desc_block = f"description: {description!r}\n" if description else ""
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n" + desc_block + "schema:\n"
        "  input: {query: string}\n"
        "  output: {result: string}\n"
        "implementation:\n"
        "  kind: python\n"
        f"  entry: {name}.impl:run\n" + tags_block
    )
    (skill_dir / "impl.py").write_text("def run(input, ctx):\n    return {'result': 'ok'}\n")
    return skill_dir


def test_search_no_filters_returns_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without filters, search renders all registered skills."""
    _write_tagged_skill(tmp_path, "alpha", tags=["crm"])
    _write_tagged_skill(tmp_path, "beta", tags=["finance"])
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(app, ["skills", "search", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "alpha" in cleaned
    assert "beta" in cleaned


def test_search_filter_by_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--tag crm should return only alpha, not beta."""
    _write_tagged_skill(tmp_path, "alpha", tags=["crm"])
    _write_tagged_skill(tmp_path, "beta", tags=["finance"])
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(app, ["skills", "search", "--tag", "crm", "--project", str(tmp_path)])
    assert result.exit_code == 0
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "alpha" in cleaned
    assert "beta" not in cleaned


def test_search_filter_by_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--query lookup should match description containing 'lookup'."""
    _write_tagged_skill(tmp_path, "alpha", tags=[], description="A lookup utility")
    _write_tagged_skill(tmp_path, "beta", tags=[], description="Something else")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(
        app, ["skills", "search", "--query", "lookup", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "alpha" in cleaned
    assert "beta" not in cleaned


def test_search_filter_by_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--kind python returns only python-backed skills."""
    _write_tagged_skill(tmp_path, "alpha", tags=[], kind="python")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(
        app, ["skills", "search", "--kind", "python", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "alpha" in cleaned


def test_search_no_matches_prints_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If no skills match, a 'no skills matched' message is printed."""
    _write_tagged_skill(tmp_path, "alpha", tags=["crm"])
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(
        app, ["skills", "search", "--tag", "nonexistent", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "no skills matched" in result.stdout


def test_search_empty_registry(tmp_path: Path) -> None:
    """Empty registry → friendly message, not an error."""
    result = runner.invoke(app, ["skills", "search", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "no skills registered" in result.stdout


# ---------------------------------------------------------------------------
# `mdk skills info`
# ---------------------------------------------------------------------------


def test_info_shows_skill_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """info renders name, version, kind, and schema for a known skill."""
    _write_python_skill(tmp_path, "calc")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(app, ["skills", "info", "calc", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "calc" in cleaned
    assert "python" in cleaned
    assert "0.1.0" in cleaned
    # agent.yaml usage snippet must be present.
    assert "skills:" in cleaned


def test_info_unknown_skill_exits_2(tmp_path: Path) -> None:
    """info on an unknown skill exits with code 2."""
    result = runner.invoke(app, ["skills", "info", "ghost", "--project", str(tmp_path)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "ghost" in combined or "not found" in combined


def test_info_shows_capabilities_when_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When capabilities: block is present in skill.yaml, info surfaces it."""
    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "__init__.py").write_text("")
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: my-skill\n"
        "version: 1.0.0\n"
        "schema:\n"
        "  input: {query: string}\n"
        "  output: {result: string}\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: my-skill.impl:run\n"
        "capabilities:\n"
        "  read_only: true\n"
        "  deterministic: true\n"
        "  network: false\n"
    )
    (skill_dir / "impl.py").write_text("def run(input, ctx):\n    return {'result': 'ok'}\n")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(app, ["skills", "info", "my-skill", "--project", str(tmp_path)])
    assert result.exit_code == 0
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "read_only=True" in cleaned
    assert "deterministic=True" in cleaned
    assert "network=False" in cleaned


def test_info_surfaces_readme_snippet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A README.md in the skill dir should appear in the info output."""
    _write_python_skill(tmp_path, "rskill")
    (tmp_path / "skills" / "rskill" / "README.md").write_text(
        "# rskill\nThis skill does something great.\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(app, ["skills", "info", "rskill", "--project", str(tmp_path)])
    assert result.exit_code == 0
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "rskill" in cleaned
    assert "README" in cleaned or "readme" in cleaned.lower()


# ---------------------------------------------------------------------------
# `mdk skills validate`
# ---------------------------------------------------------------------------


def test_validate_no_agents_dir(tmp_path: Path) -> None:
    """No agents/ directory → clean message, exit 0."""
    result = runner.invoke(app, ["skills", "validate", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "no agents" in result.stdout.lower() or "nothing to validate" in result.stdout.lower()


def test_validate_agent_with_no_skills(tmp_path: Path) -> None:
    """An agent that declares no skills passes immediately."""
    agent_dir = tmp_path / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: my-agent\n"
        "version: 1.0.0\n"
        "model:\n"
        "  provider: openai/gpt-4o\n"
        "prompt: prompt.md\n"
        "schema:\n"
        "  input: {message: string}\n"
        "  output: {reply: string}\n"
    )
    (agent_dir / "prompt.md").write_text("Answer: {{ message }}\n")
    result = runner.invoke(app, ["skills", "validate", "--project", str(tmp_path)])
    assert result.exit_code == 0
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "my-agent" in cleaned


def test_validate_skill_not_in_registry(tmp_path: Path) -> None:
    """Agent references a skill that doesn't exist → exit 2."""
    agent_dir = tmp_path / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: my-agent\n"
        "version: 1.0.0\n"
        "model:\n"
        "  provider: openai/gpt-4o\n"
        "prompt: prompt.md\n"
        "schema:\n"
        "  input: {message: string}\n"
        "  output: {reply: string}\n"
        "skills:\n"
        "  - ghost-skill\n"
    )
    (agent_dir / "prompt.md").write_text("Answer:\n")
    result = runner.invoke(app, ["skills", "validate", "--project", str(tmp_path)])
    assert result.exit_code == 2
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "ghost-skill" in cleaned
    assert "not found in skill registry" in cleaned


def test_validate_skill_output_field_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prompt references {{calc.answer}} but skill output has 'result' → fail."""
    _write_python_skill(tmp_path, "calc")  # output schema: {result: string}
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))

    agent_dir = tmp_path / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: my-agent\n"
        "version: 1.0.0\n"
        "model:\n"
        "  provider: openai/gpt-4o\n"
        "prompt: prompt.md\n"
        "schema:\n"
        "  input: {message: string}\n"
        "  output: {reply: string}\n"
        "skills:\n"
        "  - calc\n"
    )
    # References calc.answer but output schema only has 'result'.
    (agent_dir / "prompt.md").write_text("The answer is {{ calc.answer }}\n")
    result = runner.invoke(app, ["skills", "validate", "--project", str(tmp_path)])
    assert result.exit_code == 2
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "calc" in cleaned
    assert "answer" in cleaned


def test_validate_skill_output_field_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prompt references {{calc.result}} and skill output has 'result' → pass."""
    _write_python_skill(tmp_path, "calc")  # output schema: {result: string}
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))

    agent_dir = tmp_path / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: my-agent\n"
        "version: 1.0.0\n"
        "model:\n"
        "  provider: openai/gpt-4o\n"
        "prompt: prompt.md\n"
        "schema:\n"
        "  input: {message: string}\n"
        "  output: {reply: string}\n"
        "skills:\n"
        "  - calc\n"
    )
    (agent_dir / "prompt.md").write_text("The answer is {{ calc.result }}\n")
    result = runner.invoke(app, ["skills", "validate", "--project", str(tmp_path)])
    assert result.exit_code == 0
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "calc" in cleaned
    assert "✓" in cleaned or "passed" in cleaned


def test_validate_single_agent_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing an agent name validates only that agent."""
    _write_python_skill(tmp_path, "calc")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))

    agent_dir = tmp_path / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: my-agent\n"
        "version: 1.0.0\n"
        "model:\n"
        "  provider: openai/gpt-4o\n"
        "prompt: prompt.md\n"
        "schema:\n"
        "  input: {message: string}\n"
        "  output: {reply: string}\n"
        "skills:\n"
        "  - calc\n"
    )
    (agent_dir / "prompt.md").write_text("Use {{ calc.result }}\n")
    result = runner.invoke(app, ["skills", "validate", "my-agent", "--project", str(tmp_path)])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# SkillCapabilities model
# ---------------------------------------------------------------------------


def test_skill_spec_loads_capabilities_block(tmp_path: Path) -> None:
    """A skill.yaml with capabilities: parses into SkillCapabilities correctly."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("")
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: my-skill\n"
        "version: 1.0.0\n"
        "schema:\n"
        "  input: {query: string}\n"
        "  output: {result: string}\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: my-skill.impl:run\n"
        "capabilities:\n"
        "  read_only: true\n"
        "  deterministic: false\n"
        "  network: false\n"
        "  mutating: false\n"
    )
    (skill_dir / "impl.py").write_text("def run(input, ctx):\n    return {'result': 'ok'}\n")
    bundle = load_skill(skill_dir)
    caps = bundle.spec.capabilities
    assert caps.read_only is True
    assert caps.deterministic is False
    assert caps.network is False
    assert caps.mutating is False


def test_skill_spec_capabilities_defaults_to_all_none(tmp_path: Path) -> None:
    """Omitting capabilities: block leaves all flags as None (backward-compat)."""
    skill_dir = tmp_path / "plain-skill"
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("")
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        "name: plain-skill\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {query: string}\n"
        "  output: {result: string}\n"
        "implementation:\n"
        "  kind: python\n"
        "  entry: plain-skill.impl:run\n"
    )
    (skill_dir / "impl.py").write_text("def run(input, ctx):\n    return {'result': 'ok'}\n")
    bundle = load_skill(skill_dir)
    caps = bundle.spec.capabilities
    assert caps.read_only is None
    assert caps.deterministic is None
    assert caps.network is None
    assert caps.mutating is None
