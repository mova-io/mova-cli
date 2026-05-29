"""ADR 029 — workflow authoring tests.

Covers the workflow-shape detection, the planner, and the full
``mdk init <name> --llm "..."`` workflow-scaffold flow against the
deterministic ``MockProvider`` so the suite is hermetic.

What's tested:

1. **detect_workflow_shape** — pure-Python classifier. Single-agent
   descriptions must NOT misclassify; explicit step / arrow / multi-
   ``then`` / compound-marker descriptions must.
2. **plan_workflow_graph** — derives 2-4 sensible node names from
   three hand-crafted descriptions; honors the ``max_nodes`` cap.
3. **CLI end-to-end** — `mdk init <name> --llm "<workflow desc>"
   --mock` writes the canonical workflow layout (workflow.yaml +
   state.json + agents/<node>/... + evals/dataset.jsonl), the
   workflow compiles cleanly via :func:`load_workflow_spec` +
   :func:`compile_workflow`, and the post-scaffold smoke eval against
   the mock provider returns a non-zero pass rate (proving the
   scaffold isn't degenerate).
4. **--shape workflow** explicit override — forces workflow shape even
   when the description looks single-agent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.workflow.compiler import compile_workflow
from movate.core.workflow.spec import load_workflow_spec
from movate.scaffold import (
    detect_workflow_shape,
    plan_workflow_graph,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Unit — shape detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("description", "is_workflow"),
    [
        # Single-agent — must NOT misclassify.
        ("FAQ agent for SaaS pricing", False),
        ("summarize my emails into action items", False),
        (
            "classifier for sentiment with positive/negative/neutral labels",
            False,
        ),
        ("a chatbot that answers product questions", False),
        # Workflow — explicit step markers.
        (
            "draft a blog post: research → outline → write → edit",
            True,
        ),
        (
            "first research the topic, then outline it, then write the draft, then edit",
            True,
        ),
        ("step 1: research; step 2: outline; step 3: write", True),
        ("a multi-step pipeline of summarize and tag", True),
        ("two-step content pipeline: draft then review", True),
    ],
)
def test_detect_workflow_shape(description: str, is_workflow: bool) -> None:
    """The classifier must be precise on both sides."""
    assert detect_workflow_shape(description) == is_workflow, description


# ---------------------------------------------------------------------------
# Unit — workflow graph planner
# ---------------------------------------------------------------------------


_HAND_CRAFTED_DESCRIPTIONS = [
    (
        "draft a blog post: research → outline → write → edit",
        ["research", "outline", "write", "edit"],
    ),
    (
        "first research the topic, then outline it, then write the draft, then edit",
        ["research", "outline", "draft", "edit"],
    ),
    (
        "step 1: classify the ticket; step 2: route to the right team",
        ["classify", "route"],
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("description", "expected_names"),
    _HAND_CRAFTED_DESCRIPTIONS,
)
def test_plan_workflow_graph_hand_crafted(description: str, expected_names: list[str]) -> None:
    """Three hand-crafted descriptions should produce sensible 2-4
    node graphs with the right node order + names.
    """
    planned = plan_workflow_graph(description, max_nodes=4)
    assert len(planned) >= 2, planned
    assert len(planned) <= 4, planned
    names = [p.name for p in planned]
    assert names == expected_names, (description, names)


@pytest.mark.unit
def test_plan_workflow_graph_honors_node_cap() -> None:
    """Passing ``max_nodes=2`` truncates a 4-segment description to 2."""
    planned = plan_workflow_graph("research → outline → write → edit", max_nodes=2)
    assert len(planned) == 2
    # The last node carries the surplus intents so no info is dropped.
    assert "edit" in planned[-1].intent or "write" in planned[-1].intent


@pytest.mark.unit
def test_plan_workflow_graph_hard_max_ceiling() -> None:
    """The hard ceiling clamps callers passing ``max_nodes > 6``."""
    planned = plan_workflow_graph(
        "step 1: a; step 2: b; step 3: c; step 4: d; step 5: e; step 6: f; step 7: g; step 8: h",
        max_nodes=20,
    )
    assert len(planned) <= 6


# ---------------------------------------------------------------------------
# CLI end-to-end — mdk init --llm "<workflow>" --mock
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _bootstrap_project(tmp_path: Path) -> Path:
    """Create a minimal project workspace so workflow scaffolds land
    under ``<project>/workflows/<name>/``. ``project.yaml`` is loaded
    via :class:`ProjectConfig` (``extra='forbid'``), so the body is
    empty — defaults populate every field.
    """
    project = tmp_path / "demo-project"
    project.mkdir()
    (project / "project.yaml").write_text("# minimal demo project\n")
    return project


@pytest.mark.unit
def test_init_llm_workflow_writes_canonical_layout(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a workflow-shaped description produces a workflow
    scaffold that compiles via :func:`compile_workflow`.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        [
            "init",
            "blog-pipe",
            "--llm",
            "draft a blog post: research then outline then write then edit",
            "--mock",
            "--no-open-editor",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    workflow_dir = project / "workflows" / "blog-pipe"
    assert (workflow_dir / "workflow.yaml").is_file()
    assert (workflow_dir / "state.json").is_file()
    assert (workflow_dir / "evals" / "dataset.jsonl").is_file()

    # Per-node canonical agent layout: agent.yaml + prompt.md + schemas.
    for node in ("research", "outline", "write", "edit"):
        agent_dir = workflow_dir / "agents" / node
        assert (agent_dir / "agent.yaml").is_file(), node
        assert (agent_dir / "prompt.md").is_file(), node
        assert (agent_dir / "schema" / "input.yaml").is_file(), node
        assert (agent_dir / "schema" / "output.yaml").is_file(), node

    # The workflow loads + compiles end-to-end.
    spec, wf_dir = load_workflow_spec(workflow_dir / "workflow.yaml")
    graph = compile_workflow(spec, wf_dir)
    assert len(graph.nodes) == 4
    assert spec.entrypoint == "research"

    # state.json declares the workflow state schema.
    state = yaml.safe_load((workflow_dir / "state.json").read_text())
    assert state["type"] == "object"
    assert "properties" in state


@pytest.mark.unit
def test_init_llm_workflow_smoke_eval_runs(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-scaffold smoke eval prints a non-zero pass rate.

    Proves the scaffold isn't degenerate end-to-end: the workflow
    executes the mock provider across every node, the eval dataset
    runs, and at least one case passes (the mock is dataset-cycled
    against the union of per-node expecteds so the per-node strict
    output schemas are satisfied).
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        [
            "init",
            "wf-smoke",
            "--llm",
            "draft a blog post: research then outline then write then edit",
            "--mock",
            "--no-open-editor",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The smoke line is rendered as "smoke: X/Y workflow cases pass".
    # We don't assert an exact pass count (the mock's dataset-cycle is
    # order-sensitive); we only require >=1 pass to confirm the
    # scaffold is structurally sound + the per-node schemas match.
    match = re.search(r"smoke:\s+(\d+)/(\d+)\s+workflow cases pass", result.stdout)
    assert match, f"no smoke line in output:\n{result.stdout}"
    passed, total = int(match.group(1)), int(match.group(2))
    assert total > 0, match.group(0)
    assert passed > 0, match.group(0)


@pytest.mark.unit
def test_init_llm_shape_workflow_override(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--shape workflow`` forces workflow shape even when the
    description doesn't carry step markers — as long as the planner
    can segment it.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)

    # A description that auto-detects as single-agent but parses to
    # multiple verb-led segments under explicit override.
    result = runner.invoke(
        app,
        [
            "init",
            "explicit-wf",
            "--llm",
            "research the topic, outline the sections, write the post, edit",
            "--shape",
            "workflow",
            "--mock",
            "--no-open-editor",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (project / "workflows" / "explicit-wf" / "workflow.yaml").is_file()
    assert (project / "workflows" / "explicit-wf" / "agents").is_dir()


@pytest.mark.unit
def test_init_llm_shape_single_agent_override(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--shape single-agent`` forces single-agent scaffold even when
    the description reads like a workflow. Operator gets out from under
    a misclassification with a flag.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        [
            "init",
            "forced-agent",
            "--llm",
            "draft a blog post: research then outline then write then edit",
            "--shape",
            "single-agent",
            "--mock",
            "--no-open-editor",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Goes to agents/, not workflows/.
    assert (project / "agents" / "forced-agent" / "agent.yaml").is_file()
    assert not (project / "workflows" / "forced-agent").exists()


@pytest.mark.unit
def test_init_llm_workflow_nodes_cap(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--workflow-nodes 2`` truncates a 4-segment description to a
    2-node workflow scaffold.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        [
            "init",
            "small-wf",
            "--llm",
            "draft a post: research then outline then write then edit",
            "--workflow-nodes",
            "2",
            "--mock",
            "--no-open-editor",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    spec, _ = load_workflow_spec(project / "workflows" / "small-wf" / "workflow.yaml")
    assert len(spec.nodes) == 2


@pytest.mark.unit
def test_init_llm_shape_invalid_value_rejected(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--shape banana`` exits 2 with a clear error rather than
    silently falling through to a default.
    """
    project = _bootstrap_project(tmp_path)
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        [
            "init",
            "bad-shape",
            "--llm",
            "anything",
            "--shape",
            "banana",
            "--mock",
            "--no-open-editor",
        ],
    )
    assert result.exit_code == 2
    assert "shape" in (result.stderr + result.stdout).lower()
