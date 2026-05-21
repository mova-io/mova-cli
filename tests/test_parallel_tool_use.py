"""Executor parallel tool-use dispatch.

When a model emits two or more tool calls in a single turn
(``parallel_tool_calls`` has 2+ entries) the executor must:

1. Dispatch all calls concurrently via ``asyncio.gather``.
2. Append ONE assistant message carrying ALL calls.
3. Append one tool-result message PER call (with matching call ids).
4. Re-prompt the provider with the full history so it can produce
   a final answer.

This test uses a scripted provider (no real LLM) and two real Python
skills, so every layer of the dispatch path executes: executor →
skill_index lookup → dispatch_skill → result serialisation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest
from movate.core.skill_backend import SkillExecutionContext
from movate.providers.base import (
    CompletionRequest,
    CompletionResponse,
    ToolCallSpec,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Skill implementations used by the test agent.
# ---------------------------------------------------------------------------


def _add_one(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    return {"result": int(input["n"]) + 1}


def _double(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    return {"result": int(input["n"]) * 2}


# ---------------------------------------------------------------------------
# Scripted provider — returns canned CompletionResponse objects in order.
# ---------------------------------------------------------------------------


class _ScriptedProvider(MockProvider):
    """Scripted provider that returns canned CompletionResponse objects in order.

    Extends MockProvider so all protocol methods (pricing_key, to_tool_spec,
    stream, embed) are inherited with sensible defaults. Only ``complete`` is
    overridden to replay the scripted response queue. ``calls`` captures every
    request so tests can inspect the history the executor built.
    """

    def __init__(self, responses: list[CompletionResponse]) -> None:
        super().__init__(response='{"answer": "fallback"}')
        self._queue: list[CompletionResponse] = list(responses)
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        if not self._queue:
            raise AssertionError("scripted provider ran out of responses")
        return self._queue.pop(0)


# ---------------------------------------------------------------------------
# Project layout helpers.
# ---------------------------------------------------------------------------


def _write_two_skill_agent(project_root: Path) -> Path:
    """Build a minimal project with two Python skills + one agent."""
    for skill_name, fn_path, field_name in [
        ("add-one", "tests.test_parallel_tool_use:_add_one", "n"),
        ("double", "tests.test_parallel_tool_use:_double", "n"),
    ]:
        skill_dir = project_root / "skills" / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text(
            f"api_version: movate/v1\n"
            f"kind: Skill\n"
            f"name: {skill_name}\n"
            f"version: 0.1.0\n"
            f"description: computes something\n"
            f"schema:\n"
            f"  input:\n"
            f"    {field_name}: integer\n"
            f"  output:\n"
            f"    result: integer\n"
            f"implementation:\n"
            f"  kind: python\n"
            f"  entry: {fn_path}\n"
            f"cost:\n"
            f"  per_call_usd: 0.0\n"
        )
    agent_dir = project_root / "calc-agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: calc-agent\n"
        "version: 0.1.0\n"
        "runtime: litellm\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    question: string\n"
        "  output:\n"
        "    answer: string\n"
        "skills:\n"
        "  - add-one\n"
        "  - double\n"
    )
    (agent_dir / "prompt.md").write_text("{{ input.question }}")
    return agent_dir


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_dispatches_parallel_tool_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tool calls in one turn are both dispatched; message history is correct.

    Turn 1: model emits ``parallel_tool_calls=[add-one(n=9), double(n=4)]``.
    Executor gathers both dispatches → add-one returns 10, double returns 8.

    Turn 2: provider receives history with ONE assistant message containing
    both tool_calls and TWO tool-result messages. Model returns final answer.
    """
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_two_skill_agent(tmp_path)
    bundle = load_agent(agent_dir)
    assert {s.spec.name for s in bundle.skills} == {"add-one", "double"}

    # Turn 1: parallel tool-use turn.
    turn1_calls = [
        ToolCallSpec(name="add-one", call_id="c_add", input={"n": 9}),
        ToolCallSpec(name="double", call_id="c_dbl", input={"n": 4}),
    ]
    turn1_response = CompletionResponse(
        text="",
        kind="tool_use",
        tool_name=turn1_calls[0].name,
        tool_id=turn1_calls[0].call_id,
        tool_input=turn1_calls[0].input,
        parallel_tool_calls=turn1_calls,
    )
    # Turn 2: final answer.
    turn2_response = CompletionResponse(text='{"answer": "add-one=10, double=8"}')

    scripted = _ScriptedProvider([turn1_response, turn2_response])

    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=scripted,  # type: ignore[arg-type]
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="test",
    )

    response = await executor.execute(
        bundle,
        RunRequest(agent="calc-agent", input={"question": "compute both"}),
    )
    assert response.status == "success", response.error
    assert response.data == {"answer": "add-one=10, double=8"}

    # Two provider calls happened (one per turn).
    assert len(scripted.calls) == 2

    # Inspect the history the executor sent on turn 2.
    turn2_messages = scripted.calls[1].messages

    # The assistant message carries BOTH tool calls.
    assistant_msg = next(m for m in turn2_messages if m.role == "assistant" and m.tool_calls)
    assert assistant_msg.tool_calls is not None
    assert len(assistant_msg.tool_calls) == 2
    call_ids_in_history = {tc["id"] for tc in assistant_msg.tool_calls}
    assert call_ids_in_history == {"c_add", "c_dbl"}

    # There are TWO tool-result messages (one per call).
    tool_result_msgs = [m for m in turn2_messages if m.role == "tool"]
    assert len(tool_result_msgs) == 2
    result_by_id = {m.tool_call_id: json.loads(m.content) for m in tool_result_msgs}
    assert result_by_id["c_add"] == {"result": 10}
    assert result_by_id["c_dbl"] == {"result": 8}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_single_tool_call_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-call turns (parallel_tool_calls length 1) produce the same
    correct history as before the parallel-dispatch refactor."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_two_skill_agent(tmp_path)
    bundle = load_agent(agent_dir)

    single_call = ToolCallSpec(name="add-one", call_id="c_single", input={"n": 5})
    turn1 = CompletionResponse(
        text="",
        kind="tool_use",
        tool_name=single_call.name,
        tool_id=single_call.call_id,
        tool_input=single_call.input,
        parallel_tool_calls=[single_call],
    )
    turn2 = CompletionResponse(text='{"answer": "6"}')

    scripted = _ScriptedProvider([turn1, turn2])
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=scripted,  # type: ignore[arg-type]
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="test",
    )

    response = await executor.execute(
        bundle, RunRequest(agent="calc-agent", input={"question": "5+1?"})
    )
    assert response.status == "success", response.error
    assert response.data == {"answer": "6"}

    turn2_messages = scripted.calls[1].messages
    assistant_msg = next(m for m in turn2_messages if m.role == "assistant" and m.tool_calls)
    assert assistant_msg.tool_calls is not None
    assert len(assistant_msg.tool_calls) == 1
    assert assistant_msg.tool_calls[0]["id"] == "c_single"

    tool_result_msgs = [m for m in turn2_messages if m.role == "tool"]
    assert len(tool_result_msgs) == 1
    assert json.loads(tool_result_msgs[0].content) == {"result": 6}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_parallel_with_unknown_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If one of the parallel calls names a non-existent tool, the executor
    returns a NOT_FOUND error for that slot; the other dispatches normally.
    The run continues so the model can recover."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_two_skill_agent(tmp_path)
    bundle = load_agent(agent_dir)

    calls = [
        ToolCallSpec(name="add-one", call_id="c_good", input={"n": 7}),
        ToolCallSpec(name="no-such-skill", call_id="c_bad", input={}),
    ]
    turn1 = CompletionResponse(
        text="",
        kind="tool_use",
        tool_name=calls[0].name,
        tool_id=calls[0].call_id,
        tool_input=calls[0].input,
        parallel_tool_calls=calls,
    )
    turn2 = CompletionResponse(text='{"answer": "recovered"}')

    scripted = _ScriptedProvider([turn1, turn2])
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=scripted,  # type: ignore[arg-type]
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="test",
    )

    response = await executor.execute(
        bundle, RunRequest(agent="calc-agent", input={"question": "test bad tool"})
    )
    # Executor still completes — unknown tool is surfaced as a tool_result error,
    # not an exception that kills the run.
    assert response.status == "success", response.error

    turn2_messages = scripted.calls[1].messages
    tool_result_msgs = [m for m in turn2_messages if m.role == "tool"]
    assert len(tool_result_msgs) == 2
    result_by_id = {m.tool_call_id: json.loads(m.content) for m in tool_result_msgs}
    # Good tool returned its result.
    assert result_by_id["c_good"] == {"result": 8}
    # Bad tool returned a NOT_FOUND error dict.
    assert result_by_id["c_bad"]["error"] == "not_found"
