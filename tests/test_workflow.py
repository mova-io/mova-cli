"""Workflow IR + compiler + linear validator: spec parsing, structural rules, phase gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from movate.core.workflow import (
    EdgeKind,
    NodeType,
    WorkflowCompileError,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.spec import WorkflowSpec, WorkflowSpecLoadError
from movate.testing import scaffold_agent

# ---------------------------------------------------------------------------
# Fixtures: scaffold a 2-agent workflow on disk
# ---------------------------------------------------------------------------


_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "text": {"type": "string"},
        "message": {"type": "string"},
    },
}


def _write_workflow(
    workflow_dir: Path,
    *,
    nodes: list[dict],
    edges: list[dict],
    entrypoint: str = "first",
    state_schema: dict | None = None,
) -> Path:
    """Build a workflow.yaml + state schema file under ``workflow_dir``."""
    workflow_dir.mkdir(parents=True, exist_ok=True)
    schema_path = workflow_dir / "state.json"
    schema_path.write_text(json.dumps(state_schema or _STATE_SCHEMA))

    spec_yaml = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "demo-workflow",
        "version": "0.1.0",
        "description": "Test workflow",
        "state_schema": "./state.json",
        "entrypoint": entrypoint,
        "nodes": nodes,
        "edges": edges,
    }
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(yaml.safe_dump(spec_yaml))
    return yaml_path


def _scaffold_two_agents(parent: Path) -> tuple[Path, Path]:
    a = scaffold_agent(parent / "agents" / "first", name="first-agent")
    b = scaffold_agent(parent / "agents" / "second", name="second-agent")
    return a, b


def _linear_two_node(tmp_path: Path) -> Path:
    """Scaffold a valid linear 2-node workflow. Returns the workflow.yaml path
    (caller can grab the parent dir via ``yaml_path.parent``).
    """
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    return _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_workflow_spec_happy_path(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    assert isinstance(spec, WorkflowSpec)
    assert spec.name == "demo-workflow"
    assert spec.api_version == "movate/v1"
    assert len(spec.nodes) == 2
    assert spec.edges[0].from_id == "first"
    assert spec.edges[0].to_id == "second"
    assert parent == yaml_path.parent


@pytest.mark.unit
def test_load_workflow_spec_accepts_directory_path(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    spec, _ = load_workflow_spec(yaml_path.parent)
    assert spec.name == "demo-workflow"


@pytest.mark.unit
def test_load_workflow_spec_missing_file(tmp_path: Path) -> None:
    with pytest.raises(WorkflowSpecLoadError, match="not found"):
        load_workflow_spec(tmp_path)


@pytest.mark.unit
def test_load_workflow_spec_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    yaml_path.write_text(yaml_path.read_text() + "\nrandom_field: oops\n")
    with pytest.raises(WorkflowSpecLoadError, match="validation failed"):
        load_workflow_spec(yaml_path)


@pytest.mark.unit
def test_load_workflow_spec_rejects_wrong_api_version(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    yaml_path.write_text(yaml_path.read_text().replace("movate/v1", "movate/v2"))
    with pytest.raises(WorkflowSpecLoadError):
        load_workflow_spec(yaml_path)


@pytest.mark.unit
def test_load_workflow_spec_rejects_unknown_node_type_at_parse_time(
    tmp_path: Path,
) -> None:
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "tool", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )
    # ``tool`` is not in the NodeSpec discriminated union → Pydantic rejects.
    with pytest.raises(WorkflowSpecLoadError):
        load_workflow_spec(yaml_path)


# ---------------------------------------------------------------------------
# Compile (structural)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compile_happy_path(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    assert isinstance(graph, WorkflowGraph)
    assert graph.entrypoint == "first"
    assert set(graph.nodes) == {"first", "second"}
    assert all(isinstance(n, WorkflowNode) for n in graph.nodes.values())
    assert all(n.type is NodeType.AGENT for n in graph.nodes.values())
    assert len(graph.edges) == 1
    assert graph.edges[0].kind is EdgeKind.SEQUENTIAL
    # Refs must be absolute paths now.
    assert Path(graph.nodes["first"].ref).is_absolute()
    # State schema parsed.
    assert graph.state_schema["type"] == "object"


@pytest.mark.unit
def test_compile_rejects_duplicate_node_ids(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "first", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="duplicate"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_rejects_missing_ref(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/missing"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="ref path does not exist"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_rejects_unknown_entrypoint(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "second"}],
        entrypoint="ghost",
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match=r"entrypoint .* not in nodes"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_rejects_dangling_edges(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[{"from": "first", "to": "ghost"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="target node missing"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_rejects_self_loop(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
        ],
        edges=[
            {"from": "first", "to": "second"},
            {"from": "second", "to": "second"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="self-loop"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_detects_cycle(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf"
    a = scaffold_agent(workflow_dir / "agents" / "a", name="a-agent")
    b = scaffold_agent(workflow_dir / "agents" / "b", name="b-agent")
    c = scaffold_agent(workflow_dir / "agents" / "c", name="c-agent")
    _ = a, b, c
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
            {"id": "c", "type": "agent", "ref": "./agents/c"},
        ],
        edges=[
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
            {"from": "c", "to": "a"},
        ],
        entrypoint="a",
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="cycle"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_detects_orphan_nodes(tmp_path: Path) -> None:
    """A node not reachable from the entrypoint should fail compilation."""
    workflow_dir = tmp_path / "wf"
    _scaffold_two_agents(workflow_dir)
    third = scaffold_agent(workflow_dir / "agents" / "third", name="third-agent")
    _ = third
    yaml_path = _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {"id": "second", "type": "agent", "ref": "./agents/second"},
            {"id": "orphan", "type": "agent", "ref": "./agents/third"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="unreachable from entrypoint"):
        compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# HUMAN gate compile + validate (ADR 017 D5, PR 1)
# ---------------------------------------------------------------------------


def _agent_then_human(tmp_path: Path, *, prompt: str = "Approve this?") -> Path:
    """Scaffold a linear ``first(agent) → gate(human)`` workflow."""
    workflow_dir = tmp_path / "wf"
    scaffold_agent(workflow_dir / "agents" / "first", name="first-agent")
    return _write_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/first"},
            {
                "id": "gate",
                "type": "human",
                "prompt": prompt,
                "output_contract": ["decision"],
            },
        ],
        edges=[{"from": "first", "to": "gate"}],
    )


@pytest.mark.unit
def test_compile_accepts_human_node(tmp_path: Path) -> None:
    """The compiler builds a HUMAN node carrying its task spec in metadata,
    and the v0.3 phase gate (validate_linear) accepts it."""
    yaml_path = _agent_then_human(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    validate_linear(graph)  # must not raise
    gate = graph.nodes["gate"]
    assert gate.type is NodeType.HUMAN
    assert gate.ref == ""  # human gates carry no executable ref
    assert gate.metadata["prompt"] == "Approve this?"
    assert gate.metadata["output_contract"] == ["decision"]


@pytest.mark.unit
def test_compile_rejects_blank_human_prompt(tmp_path: Path) -> None:
    """A whitespace-only prompt passes Pydantic (min_length=1) but the
    compiler validates the task spec and rejects it with a clear error."""
    yaml_path = _agent_then_human(tmp_path, prompt="   ")
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match=r"human node 'gate': 'prompt'"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_rejects_invalid_state_schema(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    (yaml_path.parent / "state.json").write_text(json.dumps({"type": "potato"}))
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="invalid state_schema"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_compile_rejects_missing_state_schema(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    (yaml_path.parent / "state.json").unlink()
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="state_schema not found"):
        compile_workflow(spec, parent)


# ---------------------------------------------------------------------------
# IR helpers — topological_order, sources/sinks, is_linear
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_topological_order_linear(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    assert graph.topological_order() == ["first", "second"]


@pytest.mark.unit
def test_is_linear_true_for_chain(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    assert compile_workflow(spec, parent).is_linear()


@pytest.mark.unit
def test_is_linear_false_for_branching_graph_built_directly() -> None:
    """Directly construct a branching IR (bypassing compiler) and check is_linear=False."""
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="/y"),
            "c": WorkflowNode(id="c", type=NodeType.AGENT, ref="/z"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="b"),
            WorkflowEdge(from_id="a", to_id="c"),
        ],
        workflow_dir=Path("/"),
    )
    assert not g.is_linear()


@pytest.mark.unit
def test_sources_and_sinks() -> None:
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="/y"),
            "c": WorkflowNode(id="c", type=NodeType.AGENT, ref="/z"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="b"),
            WorkflowEdge(from_id="b", to_id="c"),
        ],
        workflow_dir=Path("/"),
    )
    assert g.sources() == ["a"]
    assert g.sinks() == ["c"]


# ---------------------------------------------------------------------------
# validate_linear (v0.3 phase gate)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_linear_accepts_linear_chain(tmp_path: Path) -> None:
    yaml_path = _linear_two_node(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    validate_linear(graph)  # must not raise


@pytest.mark.unit
def test_validate_linear_rejects_branching() -> None:
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="/y"),
            "c": WorkflowNode(id="c", type=NodeType.AGENT, ref="/z"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="b"),
            WorkflowEdge(from_id="a", to_id="c"),
        ],
        workflow_dir=Path("/"),
    )
    with pytest.raises(WorkflowCompileError, match="forbids branches"):
        validate_linear(g)


@pytest.mark.unit
def test_validate_linear_rejects_joining() -> None:
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="/y"),
            "c": WorkflowNode(id="c", type=NodeType.AGENT, ref="/z"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="c"),
            WorkflowEdge(from_id="b", to_id="c"),
        ],
        workflow_dir=Path("/"),
    )
    with pytest.raises(WorkflowCompileError, match="forbids joins"):
        validate_linear(g)


@pytest.mark.unit
def test_validate_linear_rejects_conditional_edges() -> None:
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="/y"),
        },
        edges=[
            WorkflowEdge(from_id="a", to_id="b", kind=EdgeKind.CONDITIONAL, condition="$.ok"),
        ],
        workflow_dir=Path("/"),
    )
    with pytest.raises(WorkflowCompileError, match="non-sequential"):
        validate_linear(g)


@pytest.mark.unit
def test_validate_linear_rejects_non_agent_nodes() -> None:
    # TOOL is still a rejected node type (ADR 017 D5 only un-gated HUMAN).
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.TOOL, ref="/y"),
        },
        edges=[WorkflowEdge(from_id="a", to_id="b")],
        workflow_dir=Path("/"),
    )
    with pytest.raises(WorkflowCompileError, match=r"type=agent.*type=human"):
        validate_linear(g)


@pytest.mark.unit
def test_validate_linear_accepts_human_gate() -> None:
    """ADR 017 D5: a HUMAN gate in a linear chain passes the v0.3 phase gate."""
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(
                id="b",
                type=NodeType.HUMAN,
                ref="",
                metadata={"prompt": "approve?", "output_contract": ["decision"]},
            ),
        },
        edges=[WorkflowEdge(from_id="a", to_id="b")],
        workflow_dir=Path("/"),
    )
    validate_linear(g)  # must not raise


@pytest.mark.unit
def test_validate_linear_rejects_zero_or_many_sources() -> None:
    """Two source nodes (a and b both have no inbound) → reject."""
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="a",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="/y"),
        },
        edges=[],
        workflow_dir=Path("/"),
    )
    with pytest.raises(WorkflowCompileError, match="exactly one source"):
        validate_linear(g)


@pytest.mark.unit
def test_validate_linear_requires_source_to_match_entrypoint() -> None:
    """Source node ≠ declared entrypoint should fail loudly."""
    g = WorkflowGraph(
        name="demo",
        version="0.1.0",
        description="",
        state_schema={"type": "object"},
        entrypoint="b",  # declared b, but source is a
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.AGENT, ref="/x"),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, ref="/y"),
        },
        edges=[WorkflowEdge(from_id="a", to_id="b")],
        workflow_dir=Path("/"),
    )
    with pytest.raises(WorkflowCompileError, match="must be the declared"):
        validate_linear(g)
