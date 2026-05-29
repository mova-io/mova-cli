"""ADR 029 — ``mdk dev`` workflow recognition tests.

Covers the minimal workflow plumbing added to ``mdk dev``:

1. **Recognition** — ``mdk dev <workflow-name>`` resolves to the
   ``workflows/<name>/`` directory inside a project, dispatches into
   the workflow loop (NOT the single-agent loop), and the initial
   validate + smoke pass runs.
2. **Edit triggers re-validate + re-eval** — mutating ``workflow.yaml``
   makes the watcher's snapshot diff, and the next dispatch runs both
   passes again. We exercise the underlying helpers directly so the
   test is hermetic + doesn't depend on real wall-clock polling.

The tests focus on the SMALL delta `mdk dev` had to add. The dominant
single-agent path is already covered by the existing
``tests/test_dev.py`` suite — we just confirm workflows route through
the new branch.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli._workflow_path import is_workflow_path
from movate.cli.dev_cmd import (
    _snapshot_workflow_mtimes,
    _validate_and_eval_workflow,
    _workflow_watched_paths,
)
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _bootstrap_project(tmp_path: Path) -> Path:
    project = tmp_path / "demo-project"
    project.mkdir()
    (project / "project.yaml").write_text("# minimal demo project\n")
    return project


def _scaffold_workflow(project: Path) -> Path:
    """Scaffold a 2-node workflow via ``mdk init --shape workflow --mock``."""
    result = runner.invoke(
        app,
        [
            "init",
            "blog-flow",
            "--llm",
            "research the topic, then write the post, then edit",
            "--shape",
            "workflow",
            "--workflow-nodes",
            "2",
            "--mock",
            "--no-open-editor",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    workflow_dir = project / "workflows" / "blog-flow"
    assert (workflow_dir / "workflow.yaml").is_file()
    return workflow_dir


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# 1. Recognition — `mdk dev <workflow>` routes through the workflow loop
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dev_recognizes_workflow_directory(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mdk dev <workflow-name>` finds the workflow under
    ``workflows/<name>/``, dispatches into the workflow loop (the
    initial compile + smoke pass runs), and exits cleanly in non-TTY.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)
    _scaffold_workflow(project)

    # Non-TTY: the workflow loop emits its summary line and exits
    # without entering the watch poll cycle.
    result = runner.invoke(
        app,
        ["dev", "blog-flow", "--mock"],
        input="",  # forces a non-interactive stdin
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Both signature lines appear: the initial compile (validate) +
    # the smoke (eval) pass.
    assert "blog-flow" in result.stdout
    assert "compiled" in result.stdout
    assert "smoke:" in result.stdout
    # The single-agent intro/menu strings should NOT appear — we routed
    # into the workflow branch.
    assert "Actions:" not in result.stdout
    assert "mdk_dev_workflow_summary" in result.stdout


# ---------------------------------------------------------------------------
# 2. Edit triggers re-validate + re-eval
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_workflow_watched_paths_include_every_node(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watch set covers workflow.yaml + state.json + every agent's
    agent.yaml + prompt.md + schemas. A change to any of these would
    register on a future poll cycle.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)
    workflow_dir = _scaffold_workflow(project)

    paths = _workflow_watched_paths(workflow_dir)
    # The workflow.yaml itself.
    assert workflow_dir / "workflow.yaml" in paths
    # The state schema.
    assert workflow_dir / "state.json" in paths
    # Every constituent agent's core files.
    for node in ("research", "write"):
        assert workflow_dir / "agents" / node / "agent.yaml" in paths
        assert workflow_dir / "agents" / node / "prompt.md" in paths


@pytest.mark.unit
def test_edit_workflow_yaml_changes_mtime_snapshot(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing workflow.yaml flips its mtime; the snapshot comparison
    must register that as a change. That's the trigger the dev loop
    keys on to re-validate + re-eval.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)
    workflow_dir = _scaffold_workflow(project)

    paths = _workflow_watched_paths(workflow_dir)
    before = _snapshot_workflow_mtimes(paths)

    # Touch workflow.yaml so its mtime advances. We sleep a tiny amount
    # so the new mtime exceeds the macOS HFS+/APFS 1ms resolution.
    wf_yaml = workflow_dir / "workflow.yaml"
    time.sleep(0.05)
    wf_yaml.write_text(wf_yaml.read_text() + "\n# edit\n")

    after = _snapshot_workflow_mtimes(paths)
    assert before != after
    assert after[wf_yaml] != before[wf_yaml]


@pytest.mark.unit
def test_validate_and_eval_workflow_succeeds_after_scaffold(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Calling the dev loop's validate+eval helper directly should
    print BOTH the compile-success line and the smoke pass-rate line
    for a freshly-scaffolded workflow. This is the on-edit dispatch
    the live loop runs.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)
    workflow_dir = _scaffold_workflow(project)

    _validate_and_eval_workflow(workflow_dir, mock=True)
    out = capsys.readouterr().out
    assert "compiled" in out
    assert "smoke:" in out


@pytest.mark.unit
def test_validate_and_eval_workflow_surfaces_invalid_spec(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A broken workflow.yaml (missing entrypoint) must NOT crash the
    dev loop — the helper prints the error and returns so the next
    edit can fix it.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)
    workflow_dir = _scaffold_workflow(project)

    # Corrupt workflow.yaml.
    wf_yaml = workflow_dir / "workflow.yaml"
    wf_yaml.write_text("api_version: movate/v1\nkind: Workflow\nname: broken\n")

    _validate_and_eval_workflow(workflow_dir, mock=True)
    err_out = capsys.readouterr()
    combined = err_out.out + err_out.err
    # Failure surfaced — no Python traceback, just a clean error line.
    assert (
        "validation failed" in combined.lower()
        or "load failed" in combined.lower()
        or "validation" in combined.lower()
    )


# ---------------------------------------------------------------------------
# 3. is_workflow_path — basic sanity (the dev() branch keys on this)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_workflow_path_recognizes_scaffolded_workflow(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`is_workflow_path` is what `dev()` branches on. The scaffolded
    workflow directory must report as a workflow; the per-node agent
    directory must NOT.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)
    workflow_dir = _scaffold_workflow(project)

    assert is_workflow_path(workflow_dir) is True
    assert is_workflow_path(workflow_dir / "agents" / "research") is False
