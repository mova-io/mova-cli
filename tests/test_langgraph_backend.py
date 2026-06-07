"""Tests for the LangGraph in-process execution backend (ADR 030 D1).

All tests are hermetic: no network, no real LLM — MockProvider returns
deterministic responses so assertions are stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

langgraph = pytest.importorskip("langgraph", reason="requires mdk[langgraph]")

from movate.core.executor import Executor  # noqa: E402
from movate.core.models import WorkflowStatus  # noqa: E402
from movate.core.workflow.compiler import compile_workflow  # noqa: E402
from movate.core.workflow.spec import load_workflow_spec  # noqa: E402
from movate.providers.mock import MockProvider  # noqa: E402
from movate.providers.pricing import load_pricing  # noqa: E402
from movate.runtime.langgraph_backend import (  # noqa: E402
    LangGraphBackendError,
    run_langgraph_workflow,
)
from movate.testing import InMemoryStorage, NullTracer  # noqa: E402

# ── Shared state schema ─────────────────────────────────────────────────

_STATE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "input": {"type": "string"},
        "step1_out": {"type": "string"},
        "step2_out": {"type": "string"},
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_agent(
    agent_dir: Path,
    *,
    name: str,
    input_key: str,
    output_key: str,
) -> Path:
    """Build a minimal agent that reads ``input_key`` and writes ``output_key``."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)

    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "description": f"reads {input_key}, writes {output_key}",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(
        "echo {{ input." + input_key + " }} as " + output_key + "\n"
    )
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": [input_key],
                "properties": {input_key: {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": [output_key],
                "properties": {output_key: {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {input_key: "x"}, "expected": {output_key: "x"}}) + "\n"
    )
    return agent_dir


def _make_workflow(
    workflow_dir: Path,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    entrypoint: str = "first",
    state_schema: dict[str, Any] | None = None,
    runtime: str = "langgraph",
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(state_schema or _STATE_SCHEMA))
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "test-workflow",
                "version": "0.1.0",
                "runtime": runtime,
                "state_schema": "./state.json",
                "entrypoint": entrypoint,
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _scaffold_two_step(tmp_path: Path) -> Path:
    """input → step1 → step2 (linear, two-node)."""
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "step1",
        name="step1",
        input_key="input",
        output_key="step1_out",
    )
    _make_agent(
        workflow_dir / "agents" / "step2",
        name="step2",
        input_key="step1_out",
        output_key="step2_out",
    )
    return _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/step1"},
            {"id": "second", "type": "agent", "ref": "./agents/step2"},
        ],
        edges=[{"from": "first", "to": "second"}],
    )


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def pricing():
    return load_pricing()


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer():
    return NullTracer()


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_linear_two_agents(
    tmp_path: Path,
    pricing: Any,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Two-agent linear workflow executes both nodes and merges state."""
    yaml_path = _scaffold_two_step(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    provider = MockProvider(response='{"step1_out": "from-step1", "step2_out": "from-step2"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    result = await run_langgraph_workflow(
        graph,
        {"input": "hello"},
        executor=executor,
        tracer=tracer,
        storage=storage,
        mock=True,
    )

    assert result.status == WorkflowStatus.SUCCESS, (
        f"expected SUCCESS, got {result.status}; "
        f"error_node={result.error_node_id}, error={result.error}"
    )
    assert result.final_state is not None
    assert result.workflow_run_id


@pytest.mark.unit
async def test_single_node_workflow(
    tmp_path: Path,
    pricing: Any,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """A single-node workflow completes successfully."""
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "only",
        name="only",
        input_key="input",
        output_key="step1_out",
    )
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[{"id": "first", "type": "agent", "ref": "./agents/only"}],
        edges=[],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    provider = MockProvider(response='{"step1_out": "done"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    result = await run_langgraph_workflow(
        graph,
        {"input": "test"},
        executor=executor,
        tracer=tracer,
        storage=storage,
    )

    assert result.status == WorkflowStatus.SUCCESS


@pytest.mark.unit
async def test_human_node_rejected(
    tmp_path: Path,
    pricing: Any,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """HUMAN nodes are not supported on the LangGraph backend — should fail clearly."""
    workflow_dir = tmp_path / "wf"
    _make_agent(
        workflow_dir / "agents" / "a1",
        name="a1",
        input_key="input",
        output_key="step1_out",
    )
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "first", "type": "agent", "ref": "./agents/a1"},
            {"id": "gate", "type": "human", "prompt": "approve?"},
        ],
        edges=[{"from": "first", "to": "gate"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    provider = MockProvider(response='{"step1_out": "ok"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    with pytest.raises(LangGraphBackendError, match=r"HUMAN.*not yet supported"):
        await run_langgraph_workflow(
            graph,
            {"input": "test"},
            executor=executor,
            tracer=tracer,
            storage=storage,
        )


@pytest.mark.unit
def test_module_imports_without_langgraph() -> None:
    """The backend module imports cleanly — langgraph is lazy-loaded inside the function."""
    # If this test runs at all, the import succeeded. The langgraph SDK
    # is only imported inside run_langgraph_workflow(), not at module scope.
    from movate.runtime import langgraph_backend  # noqa: PLC0415

    assert hasattr(langgraph_backend, "run_langgraph_workflow")


@pytest.mark.unit
def test_require_langgraph_extra_raises_without_sdk(monkeypatch: Any) -> None:
    """_require_langgraph_extra raises a clear error when the SDK is missing."""
    import movate.runtime.workflow_backend as wb  # noqa: PLC0415

    # Simulate the import failing.
    original = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[union-attr]

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "langgraph":
            raise ImportError("no langgraph")
        return original(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(wb.WorkflowBackendError, match=r"langgraph.*not installed"):
        wb._require_langgraph_extra()
