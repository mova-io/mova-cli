"""Core diagnoser transform + parsing (ADR 043 D1).

Covers the pure logic in :mod:`movate.core.diagnoser`:

* Empty failures → empty result with zero cost (no LLM call).
* The diagnoser sends ONE JSON-mode prompt per call (single-pass design).
* The LLM's JSON reply is parsed into typed :class:`FailureCluster` rows,
  each carrying a typed fix from the seven-kind taxonomy.
* ``max_clusters`` is honored at the result edge.
* ``budget_usd`` is enforced BEFORE the LLM call — no spend on over-budget
  requests.
* Unknown fix kinds are dropped (a model can't introduce a new taxonomy
  member via hallucination).

These tests use a hand-built mock provider — no network, deterministic
JSON replies. Storage isn't touched at all; the diagnoser is a pure
transform.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from movate.core.diagnoser import (
    ALLOWED_FIX_KINDS,
    Diagnoser,
    DiagnoserBudgetExceeded,
    Failure,
    FailureSource,
)
from movate.core.models import TokenUsage
from movate.providers.base import (
    CompletionRequest,
    CompletionResponse,
)


class _StubProvider:
    """Minimal :class:`BaseLLMProvider` stub that replays a canned response.

    Records every call so tests can assert the diagnoser made exactly
    one — the single-pass design contract.
    """

    name = "stub"
    version = "0.0.1"

    def __init__(self, *, reply: str, tokens: TokenUsage | None = None) -> None:
        self._reply = reply
        self._tokens = tokens or TokenUsage(input=100, output=200)
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        return CompletionResponse(text=self._reply, tokens=self._tokens)

    def stream(self, request: CompletionRequest) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def embed(self, text: str, *, model: str) -> list[float]:  # pragma: no cover
        raise NotImplementedError


def _failure(
    *,
    fid: str,
    summary: str = "test failure",
    source: FailureSource = FailureSource.RUN,
) -> Failure:
    return Failure(
        id=fid,
        source=source,
        summary=summary,
        created_at=datetime.now(UTC),
        input={"q": "ask"},
        output={"answer": "wrong"},
        error="schema_error: missing field",
    )


@pytest.mark.unit
async def test_empty_failures_returns_empty_no_cost() -> None:
    """No failures → no LLM call, zero cost, empty clusters."""
    provider = _StubProvider(reply="{}")
    diagnoser = Diagnoser(provider=provider, budget_usd=10.0)

    result = await diagnoser.diagnose([])

    assert result.clusters == []
    assert result.total_failures_examined == 0
    assert result.tokens_used == 0
    assert result.cost_usd == 0.0
    # Single-pass contract: zero failures → ZERO provider calls.
    assert provider.calls == []


@pytest.mark.unit
async def test_single_pass_one_llm_call() -> None:
    """The diagnoser makes exactly ONE provider call regardless of cluster count."""
    reply = json.dumps(
        {
            "clusters": [
                {
                    "id": "cl1",
                    "summary": "Cluster 1",
                    "example_count": 3,
                    "example_ids": ["f1", "f2"],
                    "confidence": "high",
                    "proposed_fix": {
                        "kind": "prompt_edit",
                        "payload": {
                            "before": "old",
                            "after": "new",
                            "patch_text": "@@\n- old\n+ new",
                        },
                        "rationale": "fix the prompt",
                    },
                },
                {
                    "id": "cl2",
                    "summary": "Cluster 2",
                    "example_count": 2,
                    "example_ids": ["f3"],
                    "confidence": "medium",
                    "proposed_fix": {
                        "kind": "model_swap",
                        "payload": {"provider": "anthropic/claude-haiku-4-5"},
                        "rationale": "try a stronger model",
                    },
                },
            ]
        }
    )
    provider = _StubProvider(reply=reply)
    diagnoser = Diagnoser(provider=provider, budget_usd=10.0)

    failures = [_failure(fid=f"f{i}") for i in range(1, 5)]
    result = await diagnoser.diagnose(failures)

    assert len(provider.calls) == 1
    assert len(result.clusters) == 2
    # Each cluster carries a typed fix from the allowed taxonomy.
    for c in result.clusters:
        assert c.proposed_fix.kind in ALLOWED_FIX_KINDS


@pytest.mark.unit
async def test_each_cluster_has_typed_fix() -> None:
    """Every fix kind from the seven-member taxonomy parses cleanly."""
    reply = json.dumps(
        {
            "clusters": [
                {
                    "id": f"cl{i}",
                    "summary": f"cluster {i}",
                    "example_count": 1,
                    "example_ids": ["f1"],
                    "confidence": "high",
                    "proposed_fix": {
                        "kind": kind,
                        "payload": payload,
                        "rationale": "because",
                    },
                }
                for i, (kind, payload) in enumerate(
                    [
                        (
                            "prompt_edit",
                            {"before": "a", "after": "b", "patch_text": "diff"},
                        ),
                        ("kb_ingest", {"kind": "url", "source": "https://x"}),
                        ("context_add", {"name": "policy", "body": "..."}),
                        ("context_remove", {"name": "stale"}),
                        ("model_swap", {"provider": "openai/gpt-4o"}),
                        ("temperature_change", {"delta": -0.2}),
                        ("retrieval_k_change", {"delta": 3}),
                    ]
                )
            ]
        }
    )
    provider = _StubProvider(reply=reply)
    diagnoser = Diagnoser(provider=provider, max_clusters=10, budget_usd=10.0)

    failures = [_failure(fid="f1")]
    result = await diagnoser.diagnose(failures)

    kinds = {c.proposed_fix.kind for c in result.clusters}
    assert kinds == {
        "prompt_edit",
        "kb_ingest",
        "context_add",
        "context_remove",
        "model_swap",
        "temperature_change",
        "retrieval_k_change",
    }


@pytest.mark.unit
async def test_max_clusters_cap_honored() -> None:
    """The configured ``max_clusters`` truncates the LLM's output."""
    reply = json.dumps(
        {
            "clusters": [
                {
                    "id": f"cl{i}",
                    "summary": f"cluster {i}",
                    "example_count": 1,
                    "example_ids": ["f1"],
                    "confidence": "medium",
                    "proposed_fix": {
                        "kind": "prompt_edit",
                        "payload": {"before": "a", "after": "b", "patch_text": "x"},
                        "rationale": "r",
                    },
                }
                for i in range(8)
            ]
        }
    )
    provider = _StubProvider(reply=reply)
    diagnoser = Diagnoser(provider=provider, max_clusters=3, budget_usd=10.0)

    result = await diagnoser.diagnose([_failure(fid="f1")])

    assert len(result.clusters) == 3  # capped


@pytest.mark.unit
async def test_unknown_fix_kind_dropped() -> None:
    """A hallucinated fix kind is dropped, not silently accepted."""
    reply = json.dumps(
        {
            "clusters": [
                {
                    "id": "cl1",
                    "summary": "good",
                    "example_count": 1,
                    "example_ids": ["f1"],
                    "confidence": "high",
                    "proposed_fix": {
                        "kind": "prompt_edit",
                        "payload": {"before": "a", "after": "b", "patch_text": "x"},
                        "rationale": "ok",
                    },
                },
                {
                    "id": "cl2",
                    "summary": "bogus kind",
                    "example_count": 1,
                    "example_ids": ["f1"],
                    "confidence": "high",
                    "proposed_fix": {
                        "kind": "delete_agent",  # not in the taxonomy
                        "payload": {},
                        "rationale": "no",
                    },
                },
            ]
        }
    )
    provider = _StubProvider(reply=reply)
    diagnoser = Diagnoser(provider=provider, budget_usd=10.0)

    result = await diagnoser.diagnose([_failure(fid="f1")])

    assert len(result.clusters) == 1
    assert result.clusters[0].proposed_fix.kind == "prompt_edit"


@pytest.mark.unit
async def test_budget_exceeded_raises_before_llm_call() -> None:
    """A pre-call estimate over ``budget_usd`` short-circuits — no spend."""
    provider = _StubProvider(reply="{}")
    # Tiny budget against a deliberately-fat token budget so the
    # pre-call estimate trips the gate.
    diagnoser = Diagnoser(
        provider=provider,
        budget_usd=0.0001,
        max_tokens=100_000,
        estimated_cost_per_1k_tokens=1.0,
    )

    with pytest.raises(DiagnoserBudgetExceeded):
        await diagnoser.diagnose([_failure(fid="f1")])

    # No LLM call was made — the gate fired before .complete().
    assert provider.calls == []


@pytest.mark.unit
async def test_unknown_example_ids_filtered() -> None:
    """A cluster citing a non-existent failure id drops just that id."""
    reply = json.dumps(
        {
            "clusters": [
                {
                    "id": "cl1",
                    "summary": "ok",
                    "example_count": 2,
                    "example_ids": ["f1", "ghost", "f2"],
                    "confidence": "high",
                    "proposed_fix": {
                        "kind": "context_add",
                        "payload": {"name": "policy", "body": "..."},
                        "rationale": "context fix",
                    },
                }
            ]
        }
    )
    provider = _StubProvider(reply=reply)
    diagnoser = Diagnoser(provider=provider, budget_usd=10.0)

    failures = [_failure(fid="f1"), _failure(fid="f2")]
    result = await diagnoser.diagnose(failures)

    cluster = result.clusters[0]
    assert "ghost" not in cluster.example_ids
    assert set(cluster.example_ids) == {"f1", "f2"}


@pytest.mark.unit
async def test_jsonish_markdown_fences_tolerated() -> None:
    """The parser strips ```json fences a model may add despite JSON mode."""
    raw_inner = {
        "clusters": [
            {
                "id": "cl1",
                "summary": "ok",
                "example_count": 1,
                "example_ids": ["f1"],
                "confidence": "low",
                "proposed_fix": {
                    "kind": "retrieval_k_change",
                    "payload": {"delta": 2},
                    "rationale": "boost k",
                },
            }
        ]
    }
    reply = "```json\n" + json.dumps(raw_inner) + "\n```"
    provider = _StubProvider(reply=reply)
    diagnoser = Diagnoser(provider=provider, budget_usd=10.0)

    result = await diagnoser.diagnose([_failure(fid="f1")])

    assert len(result.clusters) == 1
    assert result.clusters[0].proposed_fix.kind == "retrieval_k_change"
