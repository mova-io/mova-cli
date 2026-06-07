"""Native-vs-LangGraph conformance suite (ADR 030 D7 / ADR 055 D7).

Extends the conformance contract to the LangGraph backend: same fixture
workflows, same deterministic offline provider, assert identical terminal
``status`` + ``final_state``. Guarded by ``importorskip("langgraph")``
so the suite skips cleanly without the ``mdk[langgraph]`` extra.

Linear fixtures only (see test docstring for rationale).
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
from movate.core.workflow.runner import WorkflowResult, WorkflowRunner  # noqa: E402
from movate.core.workflow.spec import load_workflow_spec  # noqa: E402
from movate.providers.mock import MockProvider  # noqa: E402
from movate.providers.pricing import load_pricing  # noqa: E402
from movate.runtime.langgraph_backend import run_langgraph_workflow  # noqa: E402
from movate.testing import InMemoryStorage, NullTracer  # noqa: E402

# ── Deterministic offline provider ───────────────────────────────────────
# Same pattern as test_workflow_conformance.py: a MockProvider whose
# response is deterministic per label so both native and LangGraph
# backends see identical agent output.

_PRICING = load_pricing()


def _make_agent(agent_dir: Path, *, name: str, in_key: str, out_key: str) -> None:
    """Scaffold a minimal agent for conformance testing."""
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
                "description": f"{in_key}->{out_key}",
                "model": {
                    "provider": "openai/gpt-4o-mini-2024-07-18",
                    "params": {"temperature": 0.0},
                },
                "prompt": "./prompt.md",
                "schema": {"input": "./schema/input.json", "output": "./schema/output.json"},
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text(f"echo input.{in_key}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {in_key: {"type": "string"}},
                "required": [in_key],
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {out_key: {"type": "string"}},
                "required": [out_key],
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {in_key: "x"}, "expected": {out_key: "x"}}) + "\n"
    )


def _scaffold_linear(tmp_path: Path) -> Path:
    """Two-agent linear chain: step1 → step2."""
    wf = tmp_path / "wf"
    _make_agent(wf / "agents" / "s1", name="s1", in_key="input", out_key="s1_out")
    _make_agent(wf / "agents" / "s2", name="s2", in_key="s1_out", out_key="s2_out")
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "state.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "input": {"type": "string"},
                    "s1_out": {"type": "string"},
                    "s2_out": {"type": "string"},
                },
            }
        )
    )
    yaml_path = wf / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "linear",
                "version": "0.1.0",
                "state_schema": "./state.json",
                "entrypoint": "first",
                "nodes": [
                    {"id": "first", "type": "agent", "ref": "./agents/s1"},
                    {"id": "second", "type": "agent", "ref": "./agents/s2"},
                ],
                "edges": [{"from": "first", "to": "second"}],
            }
        )
    )
    return yaml_path


def _load(yaml_path: Path) -> Any:
    spec, parent = load_workflow_spec(yaml_path)
    return compile_workflow(spec, parent)


MOCK_RESPONSE = '{"s1_out": "from-s1", "s2_out": "from-s2"}'


async def _run_native(yaml_path: Path, initial_state: dict[str, Any]) -> WorkflowResult:
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=MockProvider(response=MOCK_RESPONSE),
        pricing=_PRICING,
        storage=storage,
        tracer=NullTracer(),
    )
    runner = WorkflowRunner(executor=executor, storage=storage)
    return await runner.run(_load(yaml_path), initial_state=dict(initial_state))


async def _run_langgraph(yaml_path: Path, initial_state: dict[str, Any]) -> dict[str, Any]:
    storage = InMemoryStorage()
    await storage.init()
    tracer = NullTracer()
    executor = Executor(
        provider=MockProvider(response=MOCK_RESPONSE),
        pricing=_PRICING,
        storage=storage,
        tracer=tracer,
    )
    result = await run_langgraph_workflow(
        _load(yaml_path),
        dict(initial_state),
        executor=executor,
        tracer=tracer,
        storage=storage,
    )
    assert result.status is WorkflowStatus.SUCCESS, (
        f"langgraph failed: {result.status} ({result.error})"
    )
    return result.final_state


# ── Conformance test ─────────────────────────────────────────────────────


@pytest.mark.unit
async def test_linear_chain_conformance(tmp_path: Path) -> None:
    """Linear two-node chain: native == langgraph on status + final_state.

    This is the ADR 055 D7 / ADR 030 D7 conformance assertion: same
    workflow, same provider, identical terminal state regardless of backend.
    """
    yaml_path = _scaffold_linear(tmp_path)
    initial = {"input": "hello"}

    native = await _run_native(yaml_path, initial)
    assert native.status is WorkflowStatus.SUCCESS

    lg_final = await _run_langgraph(yaml_path, initial)

    assert lg_final == native.final_state, (
        f"native != langgraph final_state\n"
        f"  native:    {native.final_state}\n"
        f"  langgraph: {lg_final}"
    )
