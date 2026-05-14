"""``mdk validate --project`` and ``mdk eval --project`` — team-level gates.

Sister to :mod:`tests.test_cli_scaffold` and :mod:`tests.test_cli_add`,
this file exercises the rolled-up commands Deva specifically asked for:
run validate (and eval) over every agent in a project with one command,
exit non-zero if any agent fails.

Coverage:

* ``--project`` mode discovers ``<root>/agents/<name>/`` dirs
* Per-agent failures don't abort the loop — every agent runs
* Summary table renders pass / fail / skip
* Exit code is non-zero if any agent fails
* Walk-up resolution from a subdirectory works
* No movate.yaml + no path → hard error
* Empty ``agents/`` → hard error
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.templates import get_template_path

runner = CliRunner(mix_stderr=False)


def _scaffold_project(root: Path, *, agents: list[tuple[str, str]] | None = None) -> Path:
    """Create a project root with a ``movate.yaml`` and N seeded agents.

    ``agents`` is a list of ``(name, template)`` tuples. Each agent is
    a copy of the named shape/role template, with ``__AGENT_NAME__``
    stamped. Returns the project root for chaining.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "movate.yaml").write_text("# test project marker\n")
    agents_dir = root / "agents"
    agents_dir.mkdir(exist_ok=True)
    for name, template in agents or []:
        src = get_template_path(template)
        dst = agents_dir / name
        shutil.copytree(src, dst)
        yaml_path = dst / "agent.yaml"
        yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return root


# ---------------------------------------------------------------------------
# mdk validate --project
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_project_all_pass(tmp_path: Path) -> None:
    """A project with 2 valid agents passes validation in one command."""
    project = _scaffold_project(
        tmp_path / "good",
        agents=[
            ("triage", "faq"),
            ("classify", "classifier"),
        ],
    )
    result = runner.invoke(app, ["validate", str(project), "--project"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "triage" in result.stdout
    assert "classify" in result.stdout
    # The "all passed" summary line.
    assert "passed" in result.stdout.lower()


@pytest.mark.unit
def test_validate_project_collects_all_failures(tmp_path: Path) -> None:
    """If one agent is broken, the loop still runs the others — the
    operator sees ALL failures in the summary rather than just the first."""
    project = _scaffold_project(
        tmp_path / "mixed",
        agents=[("good", "faq")],
    )
    # Add a broken agent: corrupt agent.yaml.
    broken = project / "agents" / "broken"
    broken.mkdir()
    (broken / "agent.yaml").write_text("this: is: not: valid: yaml:\n")

    result = runner.invoke(app, ["validate", str(project), "--project"])
    assert result.exit_code == 2
    # Both agents are mentioned (loop didn't abort early).
    assert "good" in result.stdout
    assert "broken" in result.stdout
    # The summary calls out the failure count.
    assert "fail" in result.stdout.lower()


@pytest.mark.unit
def test_validate_project_walks_up_from_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No path + --project from a subdir of the project finds the root
    via movate.yaml walk-up."""
    project = _scaffold_project(
        tmp_path / "walk",
        agents=[("walker", "faq")],
    )
    nested = project / "subdir" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["validate", "--project"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "walker" in result.stdout


@pytest.mark.unit
def test_validate_project_no_movate_yaml_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No movate.yaml in walk-up + no explicit path → exit 2 with hint."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    result = runner.invoke(app, ["validate", "--project"])
    assert result.exit_code == 2
    assert "movate.yaml" in result.stdout + result.stderr


@pytest.mark.unit
def test_validate_project_no_agents_errors(tmp_path: Path) -> None:
    """An empty project (movate.yaml but no agents/) errors clearly."""
    project = tmp_path / "empty"
    project.mkdir()
    (project / "movate.yaml").write_text("# empty project\n")

    result = runner.invoke(app, ["validate", str(project), "--project"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "no agents" in combined.lower() or "nothing to validate" in combined.lower()


@pytest.mark.unit
def test_validate_no_path_no_project_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-agent mode still requires a path."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# mdk eval --project
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_project_runs_each_agent_with_mock(tmp_path: Path) -> None:
    """A project with 2 agents runs eval against MockProvider for each.

    Uses --mock so the test is hermetic (no API keys, no network).
    Gate is set permissively (0.0) — we're testing the orchestration,
    not the scoring math (other test files cover scoring).
    """
    project = _scaffold_project(
        tmp_path / "evalled",
        agents=[
            ("triage", "faq"),
            ("classify", "classifier"),
        ],
    )
    result = runner.invoke(
        app,
        ["eval", str(project), "--project", "--mock", "--gate", "0.0"],
    )
    # We don't gate on exit code = 0 here — MockProvider returns canned
    # output that may or may not satisfy each role's strict schema. The
    # contract is: the command runs, both agents are visited, and a
    # summary table is printed. Exit code reflects whether any agent
    # missed its gate, which is orthogonal to the orchestration test.
    assert "triage" in result.stdout
    assert "classify" in result.stdout
    assert "summary" in result.stdout.lower()


@pytest.mark.unit
def test_eval_project_skips_agents_without_dataset(tmp_path: Path) -> None:
    """An agent without ``evals/dataset.jsonl`` is skipped, not failed."""
    project = _scaffold_project(
        tmp_path / "skipped",
        agents=[("withdata", "faq")],
    )
    # Add an agent without a dataset (copy faq template, delete the dataset).
    no_dataset = project / "agents" / "no-data"
    src = get_template_path("faq")
    shutil.copytree(src, no_dataset)
    yaml_path = no_dataset / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", "no-data"))
    (no_dataset / "evals" / "dataset.jsonl").unlink()

    result = runner.invoke(
        app,
        ["eval", str(project), "--project", "--mock", "--gate", "0.0"],
    )
    # The skip is mentioned in the per-agent block and the summary.
    assert "no-data" in result.stdout
    assert "skip" in result.stdout.lower() or "no dataset" in result.stdout.lower()


@pytest.mark.unit
def test_eval_project_no_movate_yaml_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same hard error as validate-project when there's no project root."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    result = runner.invoke(app, ["eval", "--project", "--mock"])
    assert result.exit_code == 2


@pytest.mark.unit
def test_eval_no_path_no_project_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-agent mode still requires a path."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["eval", "--mock"])
    assert result.exit_code == 2
