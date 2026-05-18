"""Dedicated tests for the skill-using demo templates: calc-agent + lookup-agent.

Covers:

* Both templates appear in ``list_templates()`` and the ``mdk templates list``
  command output.
* ``mdk add <template>`` scaffolds the agent AND auto-wires the bundled skill
  to ``<project>/skills/<name>/`` with the REAL implementation (not the echo
  stub).
* The scaffolded directory loads cleanly via ``load_agent`` (validate passes).
* The calculator Python impl runs correctly as a callable.
* The user-lookup HTTP skill.yaml is well-formed (kind: http, entry URL).

These tests pin behaviours that are easy to break silently:
  - The ``_PROJECT_MARKERS`` constant in ``init.py`` must include
    ``project.yaml`` so ``_relocate_bundled_skills`` finds the right
    project root when a project was created with ``mdk init --project``
    (which writes ``project.yaml``, not ``movate.yaml``).
  - The bundled skill MUST be relocated before ``_maybe_scaffold_declared_skills``
    runs, otherwise the default echo stub overwrites the real impl.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import TEMPLATES, get_template_path, list_templates

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkillUsingTemplatesRegistered:
    """Both templates must appear in the TEMPLATES registry and list_templates()."""

    def test_calc_agent_in_templates(self) -> None:
        assert "calc-agent" in TEMPLATES

    def test_lookup_agent_in_templates(self) -> None:
        assert "lookup-agent" in TEMPLATES

    def test_both_in_list_templates(self) -> None:
        names = list_templates()
        assert "calc-agent" in names
        assert "lookup-agent" in names

    def test_mdk_templates_list_shows_both(self) -> None:
        """`mdk templates list` output must mention both template names."""
        result = runner.invoke(app, ["templates", "list"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout
        assert "calc-agent" in result.stdout
        assert "lookup-agent" in result.stdout


# ---------------------------------------------------------------------------
# Template directories are complete
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "template,expected_skill,expected_skill_files",
    [
        ("calc-agent", "calculator", {"skill.yaml", "impl.py"}),
        ("lookup-agent", "user-lookup", {"skill.yaml"}),
    ],
)
def test_template_ships_bundled_skill(
    template: str,
    expected_skill: str,
    expected_skill_files: set[str],
) -> None:
    """Each skill-demo template ships a ``skills/<name>/`` directory
    with the required files.  The skill is bundled INSIDE the template
    directory so ``mdk add`` can relocate it to the project root.
    """
    template_dir = get_template_path(template)
    skill_dir = template_dir / "skills" / expected_skill
    assert skill_dir.is_dir(), f"{template}: missing bundled skill dir skills/{expected_skill}/"
    for required_file in expected_skill_files:
        assert (skill_dir / required_file).is_file(), (
            f"{template}: bundled skill missing {required_file}"
        )


@pytest.mark.unit
@pytest.mark.parametrize("template", ["calc-agent", "lookup-agent"])
def test_template_has_required_agent_files(template: str) -> None:
    """agent.yaml, prompt.md, schema/, and evals/dataset.jsonl all present."""
    template_dir = get_template_path(template)
    assert (template_dir / "agent.yaml").is_file()
    assert (template_dir / "prompt.md").is_file()
    assert (template_dir / "evals" / "dataset.jsonl").is_file()
    # Schema files use YAML format (the loader handles both .json and .yaml).
    assert (template_dir / "schema" / "input.yaml").is_file()
    assert (template_dir / "schema" / "output.yaml").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("template", ["calc-agent", "lookup-agent"])
def test_agent_yaml_declares_skill(template: str) -> None:
    """agent.yaml ``skills:`` list is non-empty — the skill wiring is declared."""
    template_dir = get_template_path(template)
    data = yaml.safe_load((template_dir / "agent.yaml").read_text())
    skills = data.get("skills") or []
    assert len(skills) >= 1, f"{template}: agent.yaml must declare at least one skill"


@pytest.mark.unit
@pytest.mark.parametrize("template", ["calc-agent", "lookup-agent"])
def test_dataset_uses_expected_key(template: str) -> None:
    """Every eval row must use 'expected' (not 'expected_output') — the eval
    engine requires this exact key."""
    import json  # noqa: PLC0415

    template_dir = get_template_path(template)
    rows = [
        json.loads(line)
        for line in (template_dir / "evals" / "dataset.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows, f"{template}: dataset.jsonl is empty"
    for i, row in enumerate(rows):
        assert "expected" in row, (
            f"{template}: row {i} uses wrong key — must be 'expected', not 'expected_output'"
        )
        assert "input" in row, f"{template}: row {i} missing 'input' key"


# ---------------------------------------------------------------------------
# Skill yaml correctness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calc_agent_skill_yaml_is_python_kind() -> None:
    """The calculator skill must declare ``kind: python``."""
    skill_yaml = get_template_path("calc-agent") / "skills" / "calculator" / "skill.yaml"
    data = yaml.safe_load(skill_yaml.read_text())
    assert data["implementation"]["kind"] == "python"
    # Entry must point to a Python callable.
    assert ":" in data["implementation"]["entry"], "Python skill entry must be 'module:func' format"


@pytest.mark.unit
def test_lookup_agent_skill_yaml_is_http_kind() -> None:
    """The user-lookup skill must declare ``kind: http`` with an entry URL."""
    skill_yaml = get_template_path("lookup-agent") / "skills" / "user-lookup" / "skill.yaml"
    data = yaml.safe_load(skill_yaml.read_text())
    assert data["implementation"]["kind"] == "http"
    entry = data["implementation"]["entry"]
    assert entry.startswith("http"), f"HTTP skill entry must be a URL, got: {entry!r}"


# ---------------------------------------------------------------------------
# Calculator impl correctness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calculator_impl_returns_correct_result() -> None:
    """The shipped calculator impl.py evaluates expressions correctly."""
    from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

    impl_path = TEMPLATES_DIR / "calc_agent" / "skills" / "calculator" / "impl.py"
    ns: dict[str, object] = {"__file__": str(impl_path)}
    exec(compile(impl_path.read_text(), str(impl_path), "exec"), ns)
    run_fn = ns["run"]  # type: ignore[index]

    import asyncio  # noqa: PLC0415

    class _FakeCtx:
        trace_id = "test"
        tenant_id = "test"
        run_id = "test"
        call_ms_budget = 5000

    result = asyncio.run(run_fn({"expression": "2 + 3"}, _FakeCtx()))
    assert result["result"] == 5.0
    assert isinstance(result["steps"], list)
    assert len(result["steps"]) >= 1


@pytest.mark.unit
def test_calculator_impl_rejects_unsafe_calls() -> None:
    """The AST-based calculator must reject function calls + attribute access."""
    from movate.templates import TEMPLATES_DIR  # noqa: PLC0415

    impl_path = TEMPLATES_DIR / "calc_agent" / "skills" / "calculator" / "impl.py"
    ns: dict[str, object] = {"__file__": str(impl_path)}
    exec(compile(impl_path.read_text(), str(impl_path), "exec"), ns)
    run_fn = ns["run"]  # type: ignore[index]

    import asyncio  # noqa: PLC0415

    class _FakeCtx:
        trace_id = "test"
        tenant_id = "test"
        run_id = "test"
        call_ms_budget = 5000

    # Function calls are rejected.
    result = asyncio.run(run_fn({"expression": "__import__('os').getcwd()"}, _FakeCtx()))
    # The impl returns result=0.0 + a steps list describing the error.
    assert result["result"] == 0.0
    assert len(result["steps"]) >= 1


# ---------------------------------------------------------------------------
# End-to-end: mdk add in a real project
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMdkAddCalcAgent:
    """``mdk add calc-agent`` in a project-mode workspace:
    - scaffolds the agent at agents/calc-agent/
    - relocates the REAL calculator impl to project/skills/calculator/
    - passes post-scaffold validation (validates=true in the summary line)
    """

    def test_add_calc_agent_scaffolds_agent_and_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "myproject", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout

        project = tmp_path / "myproject"
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["add", "calc-agent"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr

        # Agent landed.
        agent_dir = project / "agents" / "calc-agent"
        assert (agent_dir / "agent.yaml").is_file()
        assert (agent_dir / "prompt.md").is_file()

        # Calculator skill is in the project-level skills/ dir.
        calc_skill = project / "skills" / "calculator"
        assert calc_skill.is_dir(), "calculator skill not relocated to project/skills/calculator/"
        assert (calc_skill / "skill.yaml").is_file()
        assert (calc_skill / "impl.py").is_file()

        # The impl is the REAL one (AST-based), not the echo stub.
        impl_text = (calc_skill / "impl.py").read_text()
        assert "import ast" in impl_text, (
            "scaffolded calculator/impl.py is the echo stub, not the real impl"
        )

    def test_add_calc_agent_validates_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-scaffold validation must report success."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            app,
            ["init", "--project", "myproject", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        project = tmp_path / "myproject"
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["add", "calc-agent"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "validates=true" in result.stdout, (
            f"expected validates=true in output, got:\n{result.stdout}"
        )


@pytest.mark.unit
class TestMdkAddLookupAgent:
    """``mdk add lookup-agent`` in a project-mode workspace:
    - scaffolds the agent at agents/lookup-agent/
    - relocates the user-lookup skill.yaml to project/skills/user-lookup/
    - passes post-scaffold validation
    """

    def test_add_lookup_agent_scaffolds_agent_and_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--project", "myproject", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.stdout

        project = tmp_path / "myproject"
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["add", "lookup-agent"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.stdout + result.stderr

        # Agent landed.
        agent_dir = project / "agents" / "lookup-agent"
        assert (agent_dir / "agent.yaml").is_file()

        # HTTP skill is in the project-level skills/ dir.
        skill_dir = project / "skills" / "user-lookup"
        assert skill_dir.is_dir(), "user-lookup skill not relocated to project/skills/user-lookup/"
        assert (skill_dir / "skill.yaml").is_file()

        # skill.yaml must declare kind: http.
        data = yaml.safe_load((skill_dir / "skill.yaml").read_text())
        assert data["implementation"]["kind"] == "http"

    def test_add_lookup_agent_validates_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            app,
            ["init", "--project", "myproject", "--skip-snapshot"],
            env={"COLUMNS": "200"},
        )
        project = tmp_path / "myproject"
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["add", "lookup-agent"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "validates=true" in result.stdout, (
            f"expected validates=true in output, got:\n{result.stdout}"
        )
