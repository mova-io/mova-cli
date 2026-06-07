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
    EdgeKind,
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


# ---------------------------------------------------------------------------
# LangGraph compiler growth — ADR 030 D2 (conditional / parallel / cycle)
# + D3 (typed state). No-dependency, code-generation only.
# ---------------------------------------------------------------------------


_TYPED_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "count": {"type": "integer"},
        "score": {"type": "number"},
        "done": {"type": "boolean"},
        "items": {"type": "array"},
        "meta": {"type": "object"},
    },
}


def _graph(
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
    *,
    entrypoint: str,
    state_schema: dict | None = None,
) -> WorkflowGraph:
    return WorkflowGraph(
        name="grown-flow",
        version="0.1.0",
        description="",
        state_schema=state_schema if state_schema is not None else {"type": "object"},
        entrypoint=entrypoint,
        nodes={n.id: n for n in nodes},
        edges=edges,
        workflow_dir=Path("/tmp/fake"),
    )


def _agent(nid: str) -> WorkflowNode:
    return WorkflowNode(id=nid, type=NodeType.AGENT, ref=f"/agents/{nid}")


@pytest.mark.unit
class TestLanggraphCompilerGrowth:
    def test_regression_linear_graph_emits_no_advanced_constructs(self) -> None:
        """A linear chain must compile WITHOUT any conditional/parallel/loop
        machinery — the backward-compat regression guard."""
        graph = _graph(
            [_agent("a"), _agent("b")],
            [WorkflowEdge(from_id="a", to_id="b")],
            entrypoint="a",
        )
        source = compile_langgraph(graph)
        ast.parse(source)
        assert "add_conditional_edges" not in source
        assert "__movate_eval_condition" not in source
        assert "__movate_route_" not in source
        assert "# fan-out" not in source
        assert "loop-back edge" not in source
        # Plain sequential edges + START/END framing as before.
        assert "add_edge('a', 'b')" in source
        assert "add_edge(START, 'a')" in source
        assert "add_edge('b', END)" in source

    def test_conditional_emits_add_conditional_edges_and_router(self) -> None:
        graph = _graph(
            [_agent("a"), _agent("b"), _agent("c")],
            [
                WorkflowEdge(from_id="a", to_id="b", kind=EdgeKind.CONDITIONAL, condition="$.ok"),
                WorkflowEdge(from_id="a", to_id="c", kind=EdgeKind.CONDITIONAL, condition="$.bad"),
            ],
            entrypoint="a",
        )
        source = compile_langgraph(graph)
        ast.parse(source)
        # One conditional-edges call for the branching node, driven by a router.
        assert "add_conditional_edges('a', __movate_route_a)" in source
        assert "def __movate_route_a(state: State) -> str:" in source
        # Both branch conditions are carried verbatim from the IR.
        assert "__movate_eval_condition('$.ok', state)" in source
        assert "__movate_eval_condition('$.bad', state)" in source
        # Router always has a terminating fallthrough (bounds the guard).
        assert "return END" in source

    def test_parallel_fan_out_in_emits_concurrent_siblings_and_merge(self) -> None:
        graph = _graph(
            [_agent("split"), _agent("w1"), _agent("w2"), _agent("merge")],
            [
                WorkflowEdge(from_id="split", to_id="w1", kind=EdgeKind.PARALLEL_FAN_OUT),
                WorkflowEdge(from_id="split", to_id="w2", kind=EdgeKind.PARALLEL_FAN_OUT),
                WorkflowEdge(from_id="w1", to_id="merge", kind=EdgeKind.PARALLEL_FAN_IN),
                WorkflowEdge(from_id="w2", to_id="merge", kind=EdgeKind.PARALLEL_FAN_IN),
            ],
            entrypoint="split",
        )
        source = compile_langgraph(graph)
        ast.parse(source)
        # Concurrent siblings fan out from the splitter…
        assert "add_edge('split', 'w1')  # fan-out" in source
        assert "add_edge('split', 'w2')  # fan-out" in source
        # …and converge on the merge node (fan-in / join).
        assert "add_edge('w1', 'merge')  # fan-in (merge)" in source
        assert "add_edge('w2', 'merge')  # fan-in (merge)" in source

    def test_cycle_emits_guarded_loop(self) -> None:
        """A loop (b conditionally returns to a) MUST be emitted with the
        mandatory recursion guard so it can never run away (ADR 030 D2)."""
        retry = WorkflowEdge(from_id="b", to_id="a", kind=EdgeKind.CONDITIONAL, condition="$.retry")
        done = WorkflowEdge(from_id="b", to_id="c", kind=EdgeKind.CONDITIONAL, condition="$.done")
        graph = _graph(
            [_agent("a"), _agent("b"), _agent("c")],
            [WorkflowEdge(from_id="a", to_id="b"), retry, done],
            entrypoint="a",
        )
        source = compile_langgraph(graph)
        ast.parse(source)
        # The runaway guard is present and wired into invoke().
        assert "RECURSION_LIMIT = 25" in source
        assert "'recursion_limit': RECURSION_LIMIT" in source
        # The back-edge is identified and routed.
        assert "loop-back edge" in source
        assert "add_conditional_edges('b', __movate_route_b)" in source

    def test_typed_state_generated_from_schema(self) -> None:
        """D3: state is a typed TypedDict from state_schema, not dict[str, Any]."""
        graph = _graph(
            [_agent("a"), _agent("b")],
            [WorkflowEdge(from_id="a", to_id="b")],
            entrypoint="a",
            state_schema=_TYPED_SCHEMA,
        )
        source = compile_langgraph(graph)
        ast.parse(source)
        assert "class State(TypedDict, total=False):" in source
        assert "text: str" in source
        assert "count: int" in source
        assert "score: float" in source
        assert "done: bool" in source
        assert "items: list[Any]" in source
        assert "meta: dict[str, Any]" in source

    def test_typed_state_falls_back_when_no_properties(self) -> None:
        """A schemaless workflow keeps the generic envelope (no breakage)."""
        graph = _graph(
            [_agent("a")],
            [],
            entrypoint="a",
            state_schema={"type": "object"},
        )
        source = compile_langgraph(graph)
        ast.parse(source)
        assert "input: dict[str, Any]" in source
        assert "output: dict[str, Any]" in source

    def test_generated_source_builds_real_stategraph_if_langgraph_installed(self) -> None:
        """Optional: when langgraph IS installed, the emitted source executes
        and yields a real StateGraph. Skips cleanly without the dep (keeps CI
        green — mdk ships no langgraph dependency)."""
        pytest.importorskip("langgraph")
        retry = WorkflowEdge(from_id="b", to_id="a", kind=EdgeKind.CONDITIONAL, condition="$.retry")
        done = WorkflowEdge(from_id="b", to_id="c", kind=EdgeKind.CONDITIONAL, condition="$.done")
        graph = _graph(
            [_agent("a"), _agent("b"), _agent("c")],
            [WorkflowEdge(from_id="a", to_id="b"), retry, done],
            entrypoint="a",
            state_schema=_TYPED_SCHEMA,
        )
        source = compile_langgraph(graph)
        # Exec the generated source as a REAL module (registered in sys.modules)
        # so langgraph (1.x) get_type_hints() on the generated State TypedDict can
        # resolve its annotations via the module globals — the way the exported
        # file is actually consumed (imported / `python generated.py`). A bare
        # exec into a dict leaves State.__module__ unresolvable, so Any wouldn't
        # be found (the langgraph-1.x behavior change this guards against).
        import sys as _sys  # noqa: PLC0415
        import types as _types  # noqa: PLC0415

        _mod = _types.ModuleType("_mdk_generated_langgraph")
        _sys.modules["_mdk_generated_langgraph"] = _mod
        try:
            exec(compile(source, "<generated-langgraph>", "exec"), _mod.__dict__)
            compiled = _mod.build_graph().compile()
            assert compiled is not None
        finally:
            _sys.modules.pop("_mdk_generated_langgraph", None)
