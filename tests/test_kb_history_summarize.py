"""Tests for auto-summarize older thread turns (PR-Z).

Smarter alternative to PR-U's raw budget truncation. When the
agent opts in via ``retrieval.history_summarize=true``, the
oldest turns get compressed via a small LLM into a synthetic
'earlier in conversation: ...' entry — keeping the gist of long
threads instead of dropping it entirely.

Coverage:
* Pure helper ``summarize_older_turns``:
  - keep_recent >= len(turns) → unchanged (no compression needed)
  - LLM happy path → returns [summary_turn, ...recent N]
  - LLM failure → returns input unchanged (graceful degradation)
  - keep_recent=0 / negative → safe defaults
* End-to-end via POST messages:
  - history_summarize=false (default) → raw truncation path
  - history_summarize=true + over budget → summary appears
  - history_summarize=true + under budget → no summary call
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import JobStatus, Metrics, RunRecord, TokenUsage
from movate.kb.history_summary import summarize_older_turns
from movate.runtime import build_app
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage


def _make_resp(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def mock_litellm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(litellm, "acompletion", mock)
    return mock


# ---------------------------------------------------------------------------
# Pure summarize_older_turns
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_summarize_no_op_when_keep_recent_covers_all(
    mock_litellm: AsyncMock,
) -> None:
    """``keep_recent >= len(turns)`` → no summary, no LLM call.
    Caller passes everything verbatim."""
    turns = [{"input": {"q": f"q{i}"}, "output": {"a": f"a{i}"}} for i in range(3)]
    out = await summarize_older_turns(turns, keep_recent=5)
    assert out == turns
    mock_litellm.assert_not_called()


@pytest.mark.unit
async def test_summarize_compresses_older_turns(mock_litellm: AsyncMock) -> None:
    """Happy path: 5 turns, keep_recent=2 → first 3 collapse into a
    summary entry; last 2 preserved verbatim."""
    mock_litellm.return_value = _make_resp(
        "User asked about refunds. Agent confirmed 14-day window."
    )
    turns = [{"input": {"q": f"q{i}"}, "output": {"a": f"a{i}"}} for i in range(5)]
    out = await summarize_older_turns(turns, keep_recent=2)
    assert len(out) == 3  # 1 summary + 2 recent
    assert out[0]["input"]["summary"] is True
    assert out[0]["input"]["n_turns"] == 3
    assert "refunds" in out[0]["output"]["text"]
    # Last 2 turns preserved verbatim.
    assert out[1]["input"]["q"] == "q3"
    assert out[2]["input"]["q"] == "q4"


@pytest.mark.unit
async def test_summarize_falls_back_on_llm_error(mock_litellm: AsyncMock) -> None:
    """LLM call raises → return input unchanged (graceful degradation).
    Matches the rewriter / reranker / multi-hop pattern."""
    mock_litellm.side_effect = RuntimeError("Anthropic 5xx")
    turns = [{"input": {"q": "x"}, "output": {"a": "y"}} for _ in range(5)]
    out = await summarize_older_turns(turns, keep_recent=2)
    assert out == turns


@pytest.mark.unit
async def test_summarize_falls_back_on_empty_response(
    mock_litellm: AsyncMock,
) -> None:
    """LLM returns empty content → fall back to unchanged input."""
    mock_litellm.return_value = _make_resp("")
    turns = [{"input": {"q": "x"}, "output": {"a": "y"}} for _ in range(5)]
    out = await summarize_older_turns(turns, keep_recent=2)
    assert out == turns


@pytest.mark.unit
async def test_summarize_safe_when_keep_recent_zero(mock_litellm: AsyncMock) -> None:
    """``keep_recent=0`` is valid — all turns get summarized into one
    entry. No verbatim recent turns."""
    mock_litellm.return_value = _make_resp("Compressed.")
    turns = [{"input": {"q": "x"}, "output": {"a": "y"}} for _ in range(3)]
    out = await summarize_older_turns(turns, keep_recent=0)
    assert len(out) == 1
    assert out[0]["input"]["summary"] is True


# ---------------------------------------------------------------------------
# End-to-end via POST messages endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def summary_agents_path(tmp_path: Path) -> Path:
    """Scaffold an agent with history_summarize=true + a small budget
    so the summary path triggers reliably."""
    agents = tmp_path / "agents"
    demo = agents / "verbose"
    demo.mkdir(parents=True)
    (demo / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: verbose\n"
        "version: 0.1.0\n"
        "description: history-summary demo\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n"
        "retrieval:\n"
        "  history_summarize: true\n"
        "  history_char_budget: 2000\n",
        encoding="utf-8",
    )
    (demo / "prompt.md").write_text("Hello {{ input.q }}\n", encoding="utf-8")
    schema_dir = demo / "schema"
    schema_dir.mkdir()
    (schema_dir / "input.json").write_text(
        '{"type": "object", "properties": {"q": {"type": "string"}}}',
        encoding="utf-8",
    )
    (schema_dir / "output.json").write_text(
        '{"type": "object", "properties": {"a": {"type": "string"}}}',
        encoding="utf-8",
    )
    return agents


@pytest.fixture
def summary_client(storage: InMemoryStorage, summary_agents_path: Path) -> TestClient:
    agents = scan_agents(summary_agents_path)
    return TestClient(build_app(storage, agents=agents, agents_path=summary_agents_path))


@pytest.fixture
async def summary_auth(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="summary-tests")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _seed_n_runs(
    storage: InMemoryStorage,
    *,
    thread_id: str,
    tenant_id: str,
    n: int,
    char_size: int = 1000,
) -> None:
    now = datetime.now(UTC)
    loop = asyncio.new_event_loop()
    try:
        for i in range(n):
            run = RunRecord(
                run_id=f"r{i}",
                job_id=f"j{i}",
                tenant_id=tenant_id,
                agent="verbose",
                agent_version="0.1.0",
                prompt_hash="h",
                provider="openai/gpt-4o-mini-2024-07-18",
                provider_version="1.0",
                pricing_version="2024-01-01",
                status=JobStatus.SUCCESS,
                input={"q": f"q{i}" + ("x" * char_size)},
                output={"a": f"a{i}"},
                metrics=Metrics(
                    latency_ms=50,
                    cost_usd=0.001,
                    tokens=TokenUsage(input=5, output=5),
                ),
                created_at=now + timedelta(seconds=i),
                thread_id=thread_id,
            )
            loop.run_until_complete(storage.save_run(run))
    finally:
        loop.close()


@pytest.mark.integration
def test_endpoint_summarizes_when_over_budget(
    summary_client: TestClient,
    summary_auth: dict[str, str],
    storage: InMemoryStorage,
    mock_litellm: AsyncMock,
) -> None:
    """history_summarize=true + 5 large turns over the 2000-char budget
    → summarizer fires + injected history contains a summary entry."""
    mock_litellm.return_value = _make_resp(
        "Conversation covered refund timing, SLA, and pricing tiers."
    )
    r = summary_client.post("/api/v1/threads", json={"agent": "verbose"}, headers=summary_auth)
    thread_id = r.json()["thread_id"]
    tenant_id = storage.conversation_threads[0].tenant_id
    _seed_n_runs(storage, thread_id=thread_id, tenant_id=tenant_id, n=5, char_size=1000)

    summary_client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "new"}},
        headers=summary_auth,
    )
    history = storage.jobs[-1].input["conversation_history"]
    # First entry is the summary; followed by the most recent verbatim turns.
    assert any(t.get("input", {}).get("summary") is True for t in history)
    summary = next(t for t in history if t.get("input", {}).get("summary"))
    assert "refund" in summary["output"]["text"].lower()
    # LLM called exactly once for summarization.
    assert mock_litellm.call_count == 1


@pytest.mark.integration
def test_endpoint_skips_summary_when_under_budget(
    summary_client: TestClient,
    summary_auth: dict[str, str],
    storage: InMemoryStorage,
    mock_litellm: AsyncMock,
) -> None:
    """3 small turns under the 2000-char budget → no summarizer call,
    raw turns pass through."""
    r = summary_client.post("/api/v1/threads", json={"agent": "verbose"}, headers=summary_auth)
    thread_id = r.json()["thread_id"]
    tenant_id = storage.conversation_threads[0].tenant_id
    _seed_n_runs(storage, thread_id=thread_id, tenant_id=tenant_id, n=3, char_size=20)

    summary_client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "new"}},
        headers=summary_auth,
    )
    history = storage.jobs[-1].input["conversation_history"]
    # No summary entry — turns came through verbatim.
    assert not any(t.get("input", {}).get("summary") for t in history)
    assert len(history) == 3
    mock_litellm.assert_not_called()
