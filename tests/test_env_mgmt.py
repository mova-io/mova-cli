"""Sprint O Day 8-9 — `mdk env` tests.

Layered:

1. **Discovery** — parse_env_example handles comments/blanks;
   yaml scanner catches ${VAR}+$VAR; python scanner catches
   os.environ subscript + .get; discover_env_vars merges sources
   without duplicates.
2. **check_presence** — splits refs into (missing, present); strict
   mode treats optional unset as missing; lax mode skips them.
3. **CLI** — list / check / diff render correctly, exit codes
   gate CI, --json emits parseable output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.env_mgmt import (
    EnvSource,
    EnvVarRef,
    discover_env_vars,
    parse_env_example,
)
from movate.env_mgmt.discovery import check_presence

runner = CliRunner(mix_stderr=False)


def _scaffold_project(
    root: Path,
    *,
    env_example: str = "",
    agent_yaml_content: str = "",
    skill_impl_content: str = "",
) -> Path:
    """Build a minimal project tree with optional env-discovery sources."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("# test\n")
    if env_example:
        (root / ".env.example").write_text(env_example)
    if agent_yaml_content:
        agent_dir = root / "agents" / "a1"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.yaml").write_text(agent_yaml_content)
        (agent_dir / "prompt.md").write_text("hi")
    if skill_impl_content:
        skill_dir = root / "skills" / "s1"
        skill_dir.mkdir(parents=True)
        (skill_dir / "impl.py").write_text(skill_impl_content)
        (skill_dir / "skill.yaml").write_text("name: s1\n")
    return root


# ---------------------------------------------------------------------------
# parse_env_example
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseEnvExample:
    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".env.example"
        path.write_text("")
        assert parse_env_example(path) == []

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert parse_env_example(tmp_path / "ghost") == []

    def test_comments_and_blanks_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / ".env.example"
        path.write_text("# This is a comment\n\nFOO=\n# another comment\nBAR=baz\n\n")
        refs = parse_env_example(path)
        assert [r.name for r in refs] == ["FOO", "BAR"]

    def test_valueless_means_required(self, tmp_path: Path) -> None:
        path = tmp_path / ".env.example"
        path.write_text("FOO=\n")
        refs = parse_env_example(path)
        assert refs[0].required is True
        assert refs[0].default == ""

    def test_with_default_means_optional(self, tmp_path: Path) -> None:
        path = tmp_path / ".env.example"
        path.write_text("FOO=hello\n")
        refs = parse_env_example(path)
        assert refs[0].required is False
        assert refs[0].default == "hello"

    def test_strips_surrounding_quotes(self, tmp_path: Path) -> None:
        """`FOO="hello"` → default is `hello`, not `"hello"`."""
        path = tmp_path / ".env.example"
        path.write_text('FOO="hello world"\n')
        refs = parse_env_example(path)
        assert refs[0].default == "hello world"


# ---------------------------------------------------------------------------
# discover_env_vars — multi-source merging
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscoverEnvVars:
    def test_empty_project_returns_empty(self, tmp_path: Path) -> None:
        project = _scaffold_project(tmp_path / "p")
        assert discover_env_vars(project) == []

    def test_picks_up_env_example(self, tmp_path: Path) -> None:
        project = _scaffold_project(
            tmp_path / "p",
            env_example="OPENAI_API_KEY=\n",
        )
        refs = discover_env_vars(project)
        assert len(refs) == 1
        assert refs[0].name == "OPENAI_API_KEY"
        assert EnvSource.EXAMPLE in refs[0].sources

    def test_picks_up_agent_yaml_braced_refs(self, tmp_path: Path) -> None:
        """``${VAR}`` references in agent.yaml are captured."""
        project = _scaffold_project(
            tmp_path / "p",
            agent_yaml_content=(
                "name: a1\nmodel:\n  provider: openai/${MODEL_VERSION}\n"
                "  params:\n    api_key: ${OPENAI_API_KEY}\n"
            ),
        )
        refs = discover_env_vars(project)
        names = {r.name for r in refs}
        assert "MODEL_VERSION" in names
        assert "OPENAI_API_KEY" in names

    def test_picks_up_skill_impl_environ_refs(self, tmp_path: Path) -> None:
        """Both subscript form and .get(...) form are caught."""
        project = _scaffold_project(
            tmp_path / "p",
            skill_impl_content=(
                "import os\n"
                "key = os.environ['SLACK_TOKEN']\n"
                "fallback = os.environ.get('OPTIONAL_VAR', 'default')\n"
            ),
        )
        refs = discover_env_vars(project)
        names = {r.name for r in refs}
        assert "SLACK_TOKEN" in names
        assert "OPTIONAL_VAR" in names

    def test_merges_multiple_sources_into_one_entry(self, tmp_path: Path) -> None:
        """A var present in .env.example + agent.yaml dedupes to one
        entry with both sources listed."""
        project = _scaffold_project(
            tmp_path / "p",
            env_example="OPENAI_API_KEY=\n",
            agent_yaml_content=("name: a1\nmodel:\n  params:\n    api_key: ${OPENAI_API_KEY}\n"),
        )
        refs = discover_env_vars(project)
        api_refs = [r for r in refs if r.name == "OPENAI_API_KEY"]
        assert len(api_refs) == 1
        assert EnvSource.EXAMPLE in api_refs[0].sources
        assert EnvSource.AGENT_YAML in api_refs[0].sources

    def test_env_example_wins_on_required_vs_optional(self, tmp_path: Path) -> None:
        """If .env.example marks a var optional (with default), that
        overrides the "required by default" heuristic from other sources."""
        project = _scaffold_project(
            tmp_path / "p",
            env_example="OPTIONAL=hello\n",
            skill_impl_content=("import os\nx = os.environ.get('OPTIONAL', 'x')\n"),
        )
        refs = discover_env_vars(project)
        ref = next(r for r in refs if r.name == "OPTIONAL")
        assert ref.required is False
        assert ref.default == "hello"

    def test_results_are_sorted_by_name(self, tmp_path: Path) -> None:
        project = _scaffold_project(
            tmp_path / "p",
            env_example="ZEBRA=\nAARDVARK=\nMONKEY=\n",
        )
        refs = discover_env_vars(project)
        assert [r.name for r in refs] == ["AARDVARK", "MONKEY", "ZEBRA"]


# ---------------------------------------------------------------------------
# check_presence
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckPresence:
    def test_required_unset_is_missing(self) -> None:
        refs = [EnvVarRef(name="FOO", required=True)]
        missing, present = check_presence(refs, env={})
        assert len(missing) == 1
        assert not present

    def test_required_set_is_present(self) -> None:
        refs = [EnvVarRef(name="FOO", required=True)]
        missing, present = check_presence(refs, env={"FOO": "value"})
        assert not missing
        assert len(present) == 1

    def test_empty_value_counts_as_missing(self) -> None:
        """An exported but empty var is effectively unset."""
        refs = [EnvVarRef(name="FOO", required=True)]
        missing, _present = check_presence(refs, env={"FOO": ""})
        assert len(missing) == 1

    def test_optional_unset_is_silent_in_default_mode(self) -> None:
        refs = [EnvVarRef(name="FOO", required=False, default="x")]
        missing, present = check_presence(refs, env={})
        assert not missing
        assert not present

    def test_optional_unset_is_missing_in_strict_mode(self) -> None:
        refs = [EnvVarRef(name="FOO", required=False, default="x")]
        missing, _present = check_presence(refs, env={}, strict=True)
        assert len(missing) == 1


# ---------------------------------------------------------------------------
# CLI — list
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_envs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = _scaffold_project(
        tmp_path / "p",
        env_example="OPENAI_API_KEY=\nOPTIONAL=defaultval\n",
        agent_yaml_content=(
            "name: a1\nmodel:\n  params:\n"
            "    api_key: ${OPENAI_API_KEY}\n"
            "    workspace: ${LANGFUSE_PUBLIC_KEY}\n"
        ),
    )
    monkeypatch.chdir(project)
    return project


@pytest.mark.unit
def test_cli_env_list_renders_discovered(project_with_envs: Path) -> None:
    result = runner.invoke(app, ["env", "list"])
    assert result.exit_code == 0
    assert "OPENAI_API_KEY" in result.stdout
    assert "OPTIONAL" in result.stdout
    assert "LANGFUSE_PUBLIC_KEY" in result.stdout
    # Sources column shows where each was found
    assert "env.example" in result.stdout
    assert "agent.yaml" in result.stdout


@pytest.mark.unit
def test_cli_env_list_empty_project_prints_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _scaffold_project(tmp_path / "p")
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["env", "list"])
    assert result.exit_code == 0
    assert "no env-var references" in result.stdout.lower()


@pytest.mark.unit
def test_cli_env_list_json_output_is_parseable(
    project_with_envs: Path,
) -> None:
    result = runner.invoke(app, ["env", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    names = [p["name"] for p in payload]
    assert "OPENAI_API_KEY" in names


# ---------------------------------------------------------------------------
# CLI — check
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_env_check_all_set_exits_zero(
    project_with_envs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lp-test")
    # OPTIONAL has a default → not required → OK to be unset
    result = runner.invoke(app, ["env", "check"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "all" in result.stdout.lower() and "set" in result.stdout.lower()


@pytest.mark.unit
def test_cli_env_check_missing_required_exits_one(
    project_with_envs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Don't set OPENAI_API_KEY or LANGFUSE_PUBLIC_KEY
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    result = runner.invoke(app, ["env", "check"])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.stdout


@pytest.mark.unit
def test_cli_env_check_strict_promotes_optional_to_required(
    project_with_envs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lp-test")
    # OPTIONAL still unset
    monkeypatch.delenv("OPTIONAL", raising=False)

    # Default mode: passes
    result = runner.invoke(app, ["env", "check"])
    assert result.exit_code == 0

    # Strict mode: fails (OPTIONAL is unset)
    result_strict = runner.invoke(app, ["env", "check", "--strict"])
    assert result_strict.exit_code == 1
    assert "OPTIONAL" in result_strict.stdout


@pytest.mark.unit
def test_cli_env_check_json_output_is_parseable(
    project_with_envs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = runner.invoke(app, ["env", "check", "--json"])
    assert result.exit_code == 1  # missing required
    payload = json.loads(result.stdout)
    assert payload["all_set"] is False
    assert any(e["name"] == "OPENAI_API_KEY" for e in payload["missing"])


# ---------------------------------------------------------------------------
# CLI — diff
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_env_diff_renders_three_categories(
    project_with_envs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """diff shows declared-set, declared-unset, and shell-extra rows."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    # An extra MDK-prefixed var that's NOT in the project
    monkeypatch.setenv("MDK_RANDOM_EXTRA", "x")

    result = runner.invoke(app, ["env", "diff"])
    assert result.exit_code == 0
    # All three statuses appear
    assert "set" in result.stdout.lower()
    assert "unset" in result.stdout.lower()
    assert "extra" in result.stdout.lower()
    # The extra-shell row should reference our MDK_RANDOM_EXTRA
    assert "MDK_RANDOM_EXTRA" in result.stdout


@pytest.mark.unit
def test_cli_env_diff_empty_project_clean_shell_prints_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No project refs + no MDK_ vars in shell → friendly hint."""
    project = _scaffold_project(tmp_path / "p")
    monkeypatch.chdir(project)
    # Clean every MDK-related prefix
    for prefix in ("MDK_", "MOVATE_", "OPENAI_", "ANTHROPIC_", "AZURE_", "AWS_", "LANGFUSE_"):
        for key in list(__import__("os").environ):
            if key.startswith(prefix):
                monkeypatch.delenv(key, raising=False)
    result = runner.invoke(app, ["env", "diff"])
    assert result.exit_code == 0
    assert "no env-var" in result.stdout.lower() or "no mdk" in result.stdout.lower()
