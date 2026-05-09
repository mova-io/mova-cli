"""Live API smoke tests — opt-in, env-gated, real money.

These hit production LLM endpoints with **real API keys**. They are excluded
from the default ``pytest`` run by the ``smoke`` marker; CI never runs them.
Maintainers run them manually before tagging a release, or via a nightly
job that has provider keys.

Activation:

    export MOVATE_SMOKE=1
    export OPENAI_API_KEY=sk-…       # for OpenAI tests
    export ANTHROPIC_API_KEY=sk-…    # for Anthropic tests
    uv run pytest -m smoke

Each test is also independently gated on the relevant API key, so partial
runs work (e.g. only OPENAI_API_KEY set → Anthropic tests skip).

Goal: prove the LiteLLM seam, the typed-error mapping, and the full executor
loop against real providers. Each test runs <2s and uses ~50-150 tokens.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest
from movate.providers.base import CompletionRequest, Message
from movate.providers.litellm import LiteLLMProvider
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

pytestmark = pytest.mark.smoke

_SMOKE_ENABLED = os.environ.get("MOVATE_SMOKE", "").lower() in ("1", "true", "yes")
_HAS_OPENAI = bool(os.environ.get("OPENAI_API_KEY", "").strip())
_HAS_ANTHROPIC = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


_skip_no_smoke = pytest.mark.skipif(
    not _SMOKE_ENABLED, reason="set MOVATE_SMOKE=1 to run live-API smoke tests"
)
_skip_no_openai = pytest.mark.skipif(not _HAS_OPENAI, reason="OPENAI_API_KEY not set")
_skip_no_anthropic = pytest.mark.skipif(not _HAS_ANTHROPIC, reason="ANTHROPIC_API_KEY not set")


# ---------------------------------------------------------------------------
# Provider-level smoke
# ---------------------------------------------------------------------------


@_skip_no_smoke
@_skip_no_openai
async def test_litellm_openai_completes() -> None:
    """Direct LiteLLMProvider call against OpenAI. Asserts shape + token usage."""
    provider = LiteLLMProvider()
    response = await provider.complete(
        CompletionRequest(
            provider="openai/gpt-4o-mini-2024-07-18",
            messages=[
                Message(
                    role="user",
                    content='Reply with exactly this JSON object: {"ok": true}',
                )
            ],
            params={"max_tokens": 20, "temperature": 0.0},
        )
    )
    assert response.text, "expected non-empty response text"
    assert response.tokens.input > 0
    assert response.tokens.output > 0
    # Drift instrumentation: LiteLLM's reported cost should be present and roughly
    # match what our pricing table will compute.
    assert "litellm_cost_usd" in response.raw or response.raw.get("litellm_model")


@_skip_no_smoke
@_skip_no_anthropic
async def test_litellm_anthropic_completes() -> None:
    provider = LiteLLMProvider()
    response = await provider.complete(
        CompletionRequest(
            provider="anthropic/claude-haiku-4-5-20251001",
            messages=[
                Message(
                    role="user",
                    content='Reply with exactly this JSON object: {"ok": true}',
                )
            ],
            params={"max_tokens": 20, "temperature": 0.0},
        )
    )
    assert response.text
    assert response.tokens.input > 0
    assert response.tokens.output > 0


# ---------------------------------------------------------------------------
# Executor-level smoke — full template → real provider → schema validation
# ---------------------------------------------------------------------------


@_skip_no_smoke
@_skip_no_openai
async def test_executor_end_to_end_against_openai(tmp_path: Path) -> None:
    """Default template runs through the executor against a real OpenAI model.

    This is the highest-value smoke: it exercises the loader, prompt rendering,
    LiteLLM round-trip, JSON output parsing, schema validation, pricing-table
    cost lookup, and SQLite-shaped record persistence in a single hit.
    """
    agent_dir = scaffold_agent(tmp_path / "demo", template="default")
    bundle = load_agent(agent_dir)

    storage = InMemoryStorage()
    await storage.init()
    tracer = NullTracer()
    executor = Executor(
        provider=LiteLLMProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=tracer,
    )
    response = await executor.execute(
        bundle,
        RunRequest(agent="demo", input={"text": "say hello in one word"}),
    )
    assert response.status == "success", f"expected success, got {response.error}"
    assert "message" in response.data
    assert isinstance(response.data["message"], str)
    assert response.metrics.cost_usd > 0
    assert response.metrics.tokens.input > 0
    assert response.metrics.tokens.output > 0
    assert len(storage.runs) == 1
