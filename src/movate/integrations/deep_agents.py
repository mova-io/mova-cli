"""LangChain Deep Agents integration — run Deep Agents via mdk voice + workflows.

Deep Agents (``deepagents`` package) is LangChain's production agent harness
built on LangGraph: task planning, subagent spawning, memory, MCP tools, and
a virtual filesystem. Since ``create_deep_agent()`` returns a
``CompiledStateGraph`` (LangGraph-compatible), mdk integrates at two levels:

1. **Voice adapter** — ``DeepAgentTurn`` wraps a Deep Agent as an
   ``AgentTurn`` for the voice pipeline. Speak to a Deep Agent, hear
   its response via TTS, full multi-turn memory.

2. **Workflow node** — ``deep_agent_node()`` wraps a Deep Agent as an
   async function suitable for use as a LangGraph workflow node in
   ``run_langgraph_workflow()``.

Both are thin adapters — the real capabilities (planning, tools, memory)
come from the ``deepagents`` SDK. mdk just provides the transport
(voice/API/workflow) and observability (tracing, cost metering).

Dependencies: ``deepagents`` is imported LAZILY inside the public functions.
A runtime without the package can still import this module.

Usage — voice::

    from movate.integrations.deep_agents import DeepAgentTurn, voice_deep_agent

    # Quick start: voice-enable a Deep Agent
    pipeline_events = voice_deep_agent(
        model="anthropic:claude-sonnet-4-6",
        system_prompt="You are Deva, a helpful voice assistant.",
        tools=[get_weather, search_docs],
    )

    # Or create the turn adapter directly
    turn = DeepAgentTurn(
        model="openai:gpt-4o-mini",
        system_prompt="Concise customer support agent.",
        tools=[lookup_order],
    )
    result = await turn.run("What's my order status?", on_token=print)

Usage — workflow node::

    from movate.integrations.deep_agents import deep_agent_node

    async def my_node(state):
        return await deep_agent_node(
            state,
            model="openai:gpt-4o-mini",
            system_prompt="Analyze the data.",
            input_key="question",
            output_key="analysis",
        )
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class DeepAgentTurn:
    """Wraps a LangChain Deep Agent as an ``AgentTurn`` for the voice pipeline.

    Creates the agent lazily on first ``run()`` call. Multi-turn: the agent's
    built-in memory (LangGraph checkpointer) preserves context across turns.

    Implements the same interface as ``LangGraphAgentTurn`` and
    ``OpenAIChatAgent`` — the voice pipeline doesn't know or care which
    agent implementation is behind the turn.
    """

    name = "deep-agent"
    version = "1"
    speculatable = False  # Deep Agents may run tools/write files

    def __init__(
        self,
        *,
        model: str = "openai:gpt-4o-mini",
        system_prompt: str = "You are a helpful voice assistant. Reply concisely.",
        tools: list[Any] | None = None,
        skills: list[str] | None = None,
        memory: list[str] | None = None,
        checkpointer: Any = True,
        thread_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._skills = skills
        self._memory = memory
        self._checkpointer = checkpointer
        self._thread_id = thread_id or f"mdk-deep-{id(self):x}"
        self._kwargs = kwargs
        self._agent: Any = None

    def _ensure_agent(self) -> Any:
        """Lazily create the Deep Agent on first use."""
        if self._agent is not None:
            return self._agent

        from deepagents import create_deep_agent  # noqa: PLC0415

        self._agent = create_deep_agent(
            model=self._model,
            tools=self._tools,
            system_prompt=self._system_prompt,
            skills=self._skills,
            memory=self._memory,
            checkpointer=self._checkpointer,
            **self._kwargs,
        )
        log.info(
            "deep_agent: created model=%s tools=%d thread=%s",
            self._model,
            len(self._tools),
            self._thread_id,
        )
        return self._agent

    def reset(self) -> None:
        """Clear the agent (forces re-creation on next turn)."""
        self._agent = None
        # Rotate thread_id so memory starts fresh.
        import time  # noqa: PLC0415

        self._thread_id = f"mdk-deep-{time.monotonic_ns()}"

    async def run(
        self,
        text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Run one turn of the Deep Agent.

        Returns an ``AgentTurnResult``-compatible object. Tokens are streamed
        via ``on_token`` if provided (for TTS sentence-streaming).
        """
        from movate.voice.agent_turn import (  # noqa: PLC0415
            AgentTurnError,
            AgentTurnResult,
        )

        agent = self._ensure_agent()
        config = {"configurable": {"thread_id": session_id or self._thread_id}}

        try:
            # Stream events to capture tokens as they arrive.
            collected: list[str] = []

            if on_token is not None:
                # Use astream_events for token-level streaming.
                async for event in agent.astream_events(
                    {"messages": [{"role": "user", "content": text}]},
                    config=config,
                    version="v2",
                ):
                    kind = event.get("event", "")
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            token = chunk.content
                            if isinstance(token, str):
                                collected.append(token)
                                on_token(token)
            else:
                # Non-streaming: just ainvoke.
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": text}]},
                    config=config,
                )
                # Extract the last AI message.
                messages = result.get("messages", [])
                for msg in reversed(messages):
                    content = getattr(msg, "content", "")
                    if content and getattr(msg, "type", "") == "ai":
                        collected.append(content)
                        break

            answer = "".join(collected).strip()
            return AgentTurnResult(status="ok", answer_text=answer)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("deep_agent turn failed: %s", exc)
            return AgentTurnResult(
                status="error",
                error=AgentTurnError(message=str(exc)),
            )


async def deep_agent_node(
    state: dict[str, Any],
    *,
    model: str = "openai:gpt-4o-mini",
    system_prompt: str = "You are a helpful assistant.",
    tools: list[Any] | None = None,
    input_key: str = "input",
    output_key: str = "output",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a Deep Agent as a LangGraph workflow node function.

    Reads ``state[input_key]``, passes it to the Deep Agent, and writes
    the response to ``state[output_key]``. Compatible with
    ``run_langgraph_workflow()``'s node functions.

    Usage in a LangGraph StateGraph::

        builder.add_node("analyzer", lambda state: asyncio.run(
            deep_agent_node(state, model="openai:gpt-4o", input_key="question")
        ))
    """
    from deepagents import create_deep_agent  # noqa: PLC0415

    agent = create_deep_agent(
        model=model,
        tools=tools or [],
        system_prompt=system_prompt,
        **kwargs,
    )

    user_input = state.get(input_key, "")
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": str(user_input)}]},
    )

    # Extract the last AI message content.
    messages = result.get("messages", [])
    answer = ""
    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if content and getattr(msg, "type", "") == "ai":
            answer = content
            break

    return {**state, output_key: answer}


def voice_deep_agent(
    *,
    model: str = "openai:gpt-4o-mini",
    system_prompt: str = "You are a helpful voice assistant. Reply concisely in 1-3 sentences.",
    tools: list[Any] | None = None,
    **kwargs: Any,
) -> DeepAgentTurn:
    """Create a voice-enabled Deep Agent — the simplest entry point.

    Returns a ``DeepAgentTurn`` ready for ``run_voice_pipeline(agent=turn)``.

    Example::

        from movate.integrations.deep_agents import voice_deep_agent
        from movate.voice import run_voice_pipeline, FailoverSTT, FailoverTTS

        turn = voice_deep_agent(
            model="anthropic:claude-sonnet-4-6",
            system_prompt="You are Deva, Movate's AI receptionist.",
            tools=[search_kb, lookup_order],
        )

        async for event in run_voice_pipeline(
            audio_in=mic_stream,
            stt=FailoverSTT.default(),
            agent=turn,
            tts=FailoverTTS.default(),
        ):
            handle(event)
    """
    return DeepAgentTurn(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        **kwargs,
    )
