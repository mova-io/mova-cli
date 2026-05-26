"""MockProvider — deterministic, network-free implementation of BaseLLMProvider.

Used by the smoke test suite and the ``--mock`` flag. Default response is a
minimal JSON object that satisfies the scaffolded agent template's output
schema. Override with ``MOVATE_MOCK_RESPONSE`` or the ``response=`` arg.

**Dataset-aware mode** (PR #104, May 2026): when configured with an
agent's ``evals/dataset.jsonl[*].expected`` outputs via
:meth:`configure_dataset`, the mock cycles through those expected
outputs on each ``complete()`` call. Because the eval engine
iterates the dataset in order, eval-with-mock produces
schema-conforming responses that PASS validation — closes the
demo-day annoyance where every ``mdk eval --mock <agent>`` failed
with "model output failed schema." For single-shot ``mdk run
--mock`` the mock returns the FIRST dataset row's expected (still
schema-conforming).

Special case: when the prompt looks like an LLM-as-judge prompt (contains
``Rubric:``), the mock returns a deterministic ``{"score": ..., "rationale":
"mock"}`` payload so ``--mock`` works end-to-end through ``movate eval`` and
``movate bench`` without a second env var. The judge-response path is
NOT subject to the dataset-cycle — judge prompts are independent of
the agent's own dataset rows.

**Scaffold-aware mode**: when the prompt is the ``mdk init --llm``
scaffold meta-prompt (or its retry variant), the mock synthesizes a
valid :class:`movate.scaffold.GeneratedAgent` JSON payload for a
minimal generic agent so ``mdk init --llm --mock`` produces a runnable
agent offline (no API key). Like dataset-aware mode, this fires ONLY
when no explicit ``MOVATE_MOCK_RESPONSE`` / ``response=`` was set — an
explicit override always wins.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from movate.core.models import TokenUsage
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)

_DEFAULT_RESPONSE = '{"message": "mock response"}'
_DEFAULT_JUDGE_RESPONSE = '{"score": 0.5, "rationale": "mock judge"}'
_RESPONSE_ENV = "MOVATE_MOCK_RESPONSE"
_JUDGE_RESPONSE_ENV = "MOVATE_MOCK_JUDGE_RESPONSE"

# --- Scaffold-aware mode (offline `mdk init --llm --mock`) ----------------
#
# `mdk init <name> --llm "<desc>" --mock` is advertised as the no-key /
# hermetic-CI path, but the canned `{"message": "mock response"}` fails
# `GeneratedAgent` validation (Extra inputs not permitted / agent_yaml
# required) → hard exit. To make `--mock` actually produce a runnable
# agent offline, the mock detects the scaffold meta-prompt (and its retry
# variant) and synthesizes a minimal but valid `GeneratedAgent` JSON
# payload: a generic text-in → message-out agent, mirroring the default
# template. The synthesized payload is engineered to pass BOTH
# `GeneratedAgent.model_validate` AND `load_agent()`.
#
# Detection markers are stable substrings of the scaffold prompts (see
# `movate.scaffold.llm_scaffold._META_PROMPT` / `_RETRY_PROMPT`). We key
# on phrases that no judge/dataset/user prompt would contain, so this
# never mis-fires on the existing judge (`Rubric:`) or dataset paths.
#
# CRITICAL: this synthesis ONLY fires when `self._response_is_default`
# (no explicit `MOVATE_MOCK_RESPONSE` / `response=`). An operator who set
# an explicit response — including the phase-3 tests that force-feed a
# valid `GeneratedAgent` — always wins, exactly like dataset-aware mode.
_SCAFFOLD_PROMPT_MARKERS = (
    "scaffolding a movate AI agent",
    "GENERATEDAGENT SCHEMA",
)
_SCAFFOLD_RETRY_MARKERS = (
    "failed validation",
    "GeneratedAgent JSON",
)
# The agent-name line the meta-prompt emits — we parse the requested name
# back out so the synthesized agent.yaml carries it (the CLI also coerces
# the name post-generation, so a parse miss is harmless).
_SCAFFOLD_NAME_PREFIX = "AGENT NAME:"


def _looks_like_scaffold_prompt(body: str) -> bool:
    """True if ``body`` is the LLM-scaffold meta-prompt or its retry form.

    Matches either: ALL of the meta-prompt markers, OR all of the retry
    markers. Both groups use phrases unique to the scaffold prompts so
    this never collides with judge (``Rubric:``) or agent-run prompts.
    """
    if all(marker in body for marker in _SCAFFOLD_PROMPT_MARKERS):
        return True
    return all(marker in body for marker in _SCAFFOLD_RETRY_MARKERS)


def _parse_scaffold_name(body: str, *, default: str = "mock-agent") -> str:
    """Pull the requested agent name out of the scaffold prompt.

    The meta-prompt emits an ``AGENT NAME: <name>`` line. Best-effort:
    on any parse miss we return ``default`` — the CLI coerces the
    generated ``agent_yaml.name`` to the real ``<name>`` argument anyway.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(_SCAFFOLD_NAME_PREFIX):
            candidate = stripped[len(_SCAFFOLD_NAME_PREFIX) :].strip()
            if candidate:
                return candidate
    return default


def _build_scaffold_response(name: str) -> str:
    """Return a valid ``GeneratedAgent`` JSON payload for a generic agent.

    A minimal text-in → message-out agent (same shape as the default
    template). Engineered to satisfy every HARD CONSTRAINT in the
    scaffold meta-prompt so the result passes both
    ``GeneratedAgent.model_validate`` and ``load_agent()`` — i.e.
    ``mdk init --llm --mock`` yields a runnable agent offline.
    """
    payload: dict[str, Any] = {
        "agent_yaml": {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": name,
            "version": "0.1.0",
            "description": "A generic agent scaffolded offline by the mock provider.",
            "owner": "",
            "model": {
                "provider": "openai/gpt-4o-mini-2024-07-18",
                "params": {"temperature": 0.0, "max_tokens": 512},
            },
            "prompt": "./prompt.md",
            "schema": {
                "input": "./schema/input.json",
                "output": "./schema/output.json",
            },
            "evals": {"dataset": "./evals/dataset.jsonl"},
        },
        "prompt_md": (
            "You are a helpful assistant. Respond to the user's input.\n\n"
            "Input:\n{{ input.text }}\n\n"
            'Respond with a single JSON object on one line: {"message": "<your reply>"}'
        ),
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        },
        "sample_evals": [
            {"input": {"text": "Hello!"}, "expected": {"message": "Hi there, how can I help?"}},
            {"input": {"text": "What can you do?"}, "expected": {"message": "I answer questions."}},
        ],
    }
    return json.dumps(payload)


class MockProvider(BaseLLMProvider):
    name = "mock"
    version = "0.0.1"

    def __init__(
        self,
        response: str | None = None,
        *,
        judge_response: str | None = None,
        tool_script: list[tuple[str, dict[str, object]]] | None = None,
    ) -> None:
        """Construct a deterministic mock.

        ``tool_script`` lets tests script a tool-use loop. Each entry
        is ``(tool_name, tool_input_dict)`` — when ``complete()`` is
        called with non-empty ``tools``, the mock returns the next
        entry as a ``kind="tool_use"`` response. After the script is
        exhausted, ``complete()`` returns the final ``response`` as a
        regular ``kind="final"`` reply. This mirrors how a real LLM
        decides "I need to call a tool" → "I have the result, here's
        my final answer."
        """
        # Track whether the response was EXPLICITLY overridden. Explicit
        # overrides defeat dataset-aware mode below — operators using
        # `MOVATE_MOCK_RESPONSE` (or the `response=` constructor arg)
        # clearly want a fixed canned response; respecting that lets
        # tests force-fail scenarios still work after PR #104.
        explicit_response = response is not None or _RESPONSE_ENV in os.environ
        self._response = response or os.environ.get(_RESPONSE_ENV, _DEFAULT_RESPONSE)
        self._response_is_default = not explicit_response
        self._judge_response = judge_response or os.environ.get(
            _JUDGE_RESPONSE_ENV, _DEFAULT_JUDGE_RESPONSE
        )
        # Sanity check at construction time so tests fail loud, not at runtime.
        json.loads(self._response)
        json.loads(self._judge_response)
        self._tool_script: list[tuple[str, dict[str, object]]] = list(tool_script or [])
        self._tool_calls_emitted = 0
        # Dataset-aware mode (PR #104). Populated post-construction
        # via :meth:`configure_dataset`. When non-empty AND no
        # explicit response was set, ``complete()`` cycles through
        # these on each call instead of returning the default response.
        # Explicit `MOVATE_MOCK_RESPONSE` / `response=` overrides win.
        self._dataset_expecteds: list[Any] = []
        self._dataset_call_index = 0

    def configure_dataset(self, expecteds: list[Any]) -> None:
        """Switch the mock into dataset-aware mode.

        ``expecteds`` is the list of ``dataset.jsonl[*].expected``
        outputs, in dataset order. After this call, each invocation
        of :meth:`complete` returns the next entry (cycling at end).
        Pass an empty list to reset back to the canned ``response``.

        Used by ``mdk run --mock`` and ``mdk eval --mock`` to make
        the mock produce schema-conforming outputs that match what
        the dataset says the agent SHOULD return. Without this, the
        mock returns the canned ``{"message": "mock response"}``
        which fails validation against any non-trivial output
        schema (the previous demo annoyance).
        """
        self._dataset_expecteds = list(expecteds)
        self._dataset_call_index = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content if request.messages else ""
        prompt_chars = sum(len(m.content) for m in request.messages)

        # Tool-use scripting: when the request has tools AND the script
        # still has entries, emit the next tool call. Each call gets a
        # deterministic id ``mock-tool-<n>`` so test assertions can
        # match by index. After the script is exhausted, fall through
        # to the final response below.
        if request.tools and self._tool_calls_emitted < len(self._tool_script):
            name, args = self._tool_script[self._tool_calls_emitted]
            call_id = f"mock-tool-{self._tool_calls_emitted}"
            self._tool_calls_emitted += 1
            return CompletionResponse(
                text="",
                tokens=TokenUsage(
                    input=max(1, prompt_chars // 4),
                    output=1,
                ),
                raw={"mock": True, "provider": request.provider, "tool_use": True},
                kind="tool_use",
                tool_name=name,
                tool_id=call_id,
                tool_input=args,
            )

        # Four-way choice for the response text:
        # 1. Judge prompt → canned judge-response (rubric-aware)
        # 2. Scaffold prompt → synthesized valid GeneratedAgent JSON
        #    (offline `mdk init --llm --mock`); default-response only
        # 3. Dataset-aware mode (PR #104) → next expected from dataset
        # 4. Default → canned _response
        is_judge_prompt = "Rubric:" in body
        if is_judge_prompt:
            text = self._judge_response
        elif self._response_is_default and _looks_like_scaffold_prompt(body):
            # Offline scaffold path. Only when the response wasn't
            # explicitly overridden — phase-3 tests that force-feed a
            # GeneratedAgent via MOVATE_MOCK_RESPONSE must still win.
            text = _build_scaffold_response(_parse_scaffold_name(body))
        elif self._dataset_expecteds and self._response_is_default:
            # Cycle through dataset rows in order. Wraps at the end so
            # callers that exceed dataset length still get valid output
            # (rather than IndexError or fallback to non-conforming
            # default). Skipped when the operator explicitly overrode
            # the response (env var or constructor arg) — they wanted
            # a fixed value, respect that.
            expected = self._dataset_expecteds[
                self._dataset_call_index % len(self._dataset_expecteds)
            ]
            self._dataset_call_index += 1
            # Serialize the expected dict to JSON — that's how the
            # provider's text body crosses into the schema validator.
            text = json.dumps(expected)
        else:
            text = self._response
        return CompletionResponse(
            text=text,
            tokens=TokenUsage(
                input=max(1, prompt_chars // 4),
                output=max(1, len(text) // 4),
            ),
            raw={"mock": True, "provider": request.provider},
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Deterministic streaming for tests: chunk the canned response
        into ~10-char slices, then emit a final usage-only chunk so
        cost accounting downstream sees real numbers."""
        body = request.messages[0].content if request.messages else ""
        text = self._judge_response if "Rubric:" in body else self._response
        prompt_chars = sum(len(m.content) for m in request.messages)
        # Yield in small slices so test code observing the chunks
        # actually sees a stream (more than one chunk).
        slice_size = 10
        for i in range(0, len(text), slice_size):
            yield StreamChunk(text=text[i : i + slice_size])
        # Final chunk: zero text, populated tokens (mirrors LiteLLM's
        # include_usage=True behaviour).
        yield StreamChunk(
            text="",
            tokens=TokenUsage(
                input=max(1, prompt_chars // 4),
                output=max(1, len(text) // 4),
            ),
        )

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError


def load_dataset_expecteds(dataset_path: Any) -> list[Any]:
    """Read an agent's ``evals/dataset.jsonl`` and return its
    ``expected`` outputs in order.

    Used by the CLI to switch :class:`MockProvider` into dataset-aware
    mode just before running an agent or eval. Best-effort: a
    missing / malformed dataset yields an empty list — the mock then
    falls back to its canned response.

    ``dataset_path`` is duck-typed (``pathlib.Path``-shaped object
    expected). Lazy-typed because :mod:`movate.providers.mock`
    shouldn't import pathlib just for this helper.
    """
    if dataset_path is None:
        return []
    try:
        text = dataset_path.read_text()
    except (OSError, AttributeError):
        return []
    expecteds: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            # Malformed row — skip silently. The eval engine's own
            # dataset loader surfaces the canonical error elsewhere.
            continue
        if isinstance(row, dict) and "expected" in row:
            expecteds.append(row["expected"])
    return expecteds
