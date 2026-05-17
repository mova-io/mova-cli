"""Sprint U — `mdk compose` + LangGraph compiler scaffold tests.

Two layers:

1. **`mdk compose` CLI** — scaffolds a valid workflow.yaml.
2. **LangGraph compiler scaffold** — emits parseable Python for a
   small workflow graph.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.compose_cmd import _scaffold_workflow_yaml, _slug_id
from movate.cli.main import app
from movate.core.workflow.compilers.langgraph import compile_langgraph
from movate.core.workflow.ir import (
    NodeType,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlugId:
    def test_lowercases_and_hyphenates(self) -> None:
        assert _slug_id("My Cool Flow") == "my-cool-flow"

    def test_strips_edge_hyphens(self) -> None:
        assert _slug_id("-x-") == "x"

    def test_empty_falls_back(self) -> None:
        assert _slug_id("!!!") == "workflow"


@pytest.mark.unit
class TestScaffoldWorkflowYaml:
    def test_emits_required_fields(self) -> None:
        spec = _scaffold_workflow_yaml(
            workflow_name="flow",
            agent_names=["a", "b", "c"],
            runtime="native",
            description="test",
        )
        assert spec["api_version"] == "movate/v1"
        assert spec["kind"] == "Workflow"
        assert spec["name"] == "flow"

    def test_one_node_per_agent(self) -> None:
        spec = _scaffold_workflow_yaml(
            workflow_name="flow",
            agent_names=["one", "two", "three"],
            runtime="native",
            description="",
        )
        assert len(spec["nodes"]) == 3
        ids = [n["id"] for n in spec["nodes"]]
        assert ids == ["one", "two", "three"]

    def test_edges_chain_sequentially(self) -> None:
        spec = _scaffold_workflow_yaml(
            workflow_name="flow",
            agent_names=["a", "b", "c"],
            runtime="native",
            description="",
        )
        # 3 nodes → 2 sequential edges
        assert len(spec["edges"]) == 2
        assert spec["edges"][0] == {"from": "a", "to": "b"}
        assert spec["edges"][1] == {"from": "b", "to": "c"}

    def test_single_agent_has_no_edges(self) -> None:
        spec = _scaffold_workflow_yaml(
            workflow_name="flow",
            agent_names=["solo"],
            runtime="native",
            description="",
        )
        assert spec["edges"] == []

    def test_langgraph_runtime_recorded(self) -> None:
        spec = _scaffold_workflow_yaml(
            workflow_name="flow",
            agent_names=["a"],
            runtime="langgraph",
            description="",
        )
        assert spec.get("runtime") == "langgraph"

    def test_native_runtime_omitted(self) -> None:
        """native = default = no runtime: key (cleaner YAML)."""
        spec = _scaffold_workflow_yaml(
            workflow_name="flow",
            agent_names=["a"],
            runtime="native",
            description="",
        )
        assert "runtime" not in spec


# ---------------------------------------------------------------------------
# CLI: happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_compose_writes_workflow_yaml(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "compose",
            "my-flow",
            "--agents",
            "alpha,beta,gamma",
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    target = tmp_path / "workflows" / "my-flow" / "workflow.yaml"
    assert target.is_file()
    # Generated file parses as YAML + has expected nodes
    spec = yaml.safe_load(target.read_text())
    assert spec["name"] == "my-flow"
    assert len(spec["nodes"]) == 3


@pytest.mark.unit
def test_cli_compose_custom_output_path(tmp_path: Path) -> None:
    out = tmp_path / "custom" / "flow.yaml"
    result = runner.invoke(
        app,
        [
            "compose",
            "x",
            "--agents",
            "a,b",
            "--output",
            str(out),
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert out.is_file()


@pytest.mark.unit
def test_cli_compose_langgraph_runtime(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "compose",
            "lg-flow",
            "--agents",
            "a,b",
            "--runtime",
            "langgraph",
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    spec = yaml.safe_load((tmp_path / "workflows" / "lg-flow" / "workflow.yaml").read_text())
    assert spec["runtime"] == "langgraph"


# ---------------------------------------------------------------------------
# CLI: error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_compose_missing_agents_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["compose", "x", "--project-root", str(tmp_path)])
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_compose_bad_runtime_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "compose",
            "x",
            "--agents",
            "a",
            "--runtime",
            "bogus",
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_cli_compose_refuses_existing_without_force(tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"
    out.write_text("keep me\n")
    result = runner.invoke(
        app,
        [
            "compose",
            "x",
            "--agents",
            "a",
            "--output",
            str(out),
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert out.read_text() == "keep me\n"


@pytest.mark.unit
def test_cli_compose_force_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"
    out.write_text("old\n")
    result = runner.invoke(
        app,
        [
            "compose",
            "x",
            "--agents",
            "a",
            "--output",
            str(out),
            "--force",
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    # File replaced — old content is gone, valid YAML in its place.
    text = out.read_text()
    assert "old\n" not in text
    spec = yaml.safe_load(text)
    assert spec["kind"] == "Workflow"


# ---------------------------------------------------------------------------
# LangGraph compiler scaffold
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLanggraphCompiler:
    def _make_graph(self) -> WorkflowGraph:
        """Build a tiny WorkflowGraph in-memory.

        We don't go through the full compiler.compile() path because
        it pulls in fs / spec validation; for the lang compiler test
        we construct the WorkflowGraph directly.
        """
        nodes = {
            "alpha": WorkflowNode(id="alpha", type=NodeType.AGENT, ref="../../agents/alpha"),
            "beta": WorkflowNode(id="beta", type=NodeType.AGENT, ref="../../agents/beta"),
        }
        return WorkflowGraph(
            name="demo-flow",
            version="0.1.0",
            description="",
            state_schema={"type": "object"},
            entrypoint="alpha",
            nodes=nodes,
            edges=[WorkflowEdge(from_id="alpha", to_id="beta")],
            workflow_dir=Path("/tmp/fake"),
        )

    def test_compile_returns_valid_python(self) -> None:
        graph = self._make_graph()
        source = compile_langgraph(graph)
        # The generated source must be parseable Python — any syntax
        # error in the scaffold breaks every downstream operator.
        ast.parse(source)

    def test_compile_includes_each_node_as_add_node_call(self) -> None:
        graph = self._make_graph()
        source = compile_langgraph(graph)
        # Each workflow node should appear in an add_node(...) call.
        assert "add_node('alpha'" in source
        assert "add_node('beta'" in source

    def test_compile_includes_edges(self) -> None:
        graph = self._make_graph()
        source = compile_langgraph(graph)
        # Sequential edge from alpha → beta
        assert "add_edge('alpha', 'beta')" in source
        # Plus START / END framing
        assert "add_edge(START," in source
        assert "END)" in source
