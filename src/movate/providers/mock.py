"""MockProvider — deterministic, network-free implementation of BaseLLMProvider.

Used by the smoke test suite and the ``--mock`` flag. Default response is a
minimal JSON object that satisfies the scaffolded agent template's output
schema. Override with ``MOVATE_MOCK_RESPONSE`` or the ``response=`` arg.

Special case: when the prompt looks like an LLM-as-judge prompt (contains
``Rubric:``), the mock returns a deterministic ``{"score": ..., "rationale":
"mock"}`` payload so ``--mock`` works end-to-end through ``movate eval`` and
``movate bench`` without a second env var.
"""

from __future__ import annotations

import json
import os

from movate.core.models import TokenUsage
from movate.providers.base import BaseLLMProvider, CompletionRequest, CompletionResponse

_DEFAULT_RESPONSE = '{"message": "mock response"}'
_DEFAULT_JUDGE_RESPONSE = '{"score": 0.5, "rationale": "mock judge"}'
_RESPONSE_ENV = "MOVATE_MOCK_RESPONSE"
_JUDGE_RESPONSE_ENV = "MOVATE_MOCK_JUDGE_RESPONSE"


class MockProvider(BaseLLMProvider):
    name = "mock"
    version = "0.0.1"

    def __init__(
        self,
        response: str | None = None,
        *,
        judge_response: str | None = None,
    ) -> None:
        self._response = response or os.environ.get(_RESPONSE_ENV, _DEFAULT_RESPONSE)
        self._judge_response = judge_response or os.environ.get(
            _JUDGE_RESPONSE_ENV, _DEFAULT_JUDGE_RESPONSE
        )
        # Sanity check at construction time so tests fail loud, not at runtime.
        json.loads(self._response)
        json.loads(self._judge_response)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content if request.messages else ""
        text = self._judge_response if "Rubric:" in body else self._response
        prompt_chars = sum(len(m.content) for m in request.messages)
        return CompletionResponse(
            text=text,
            tokens=TokenUsage(
                input=max(1, prompt_chars // 4),
                output=max(1, len(text) // 4),
            ),
            raw={"mock": True, "provider": request.provider},
        )

    async def stream(self, request: CompletionRequest) -> object:  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError
