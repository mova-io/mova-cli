"""Tests for per-agent retrieval-config budget overrides (PR-W).

Extends RetrievalConfig with three new optional fields:
* ``multi_hop_max_total_chunks``
* ``history_turns``
* ``history_char_budget``

Each is ``None`` by default = "use the process default". Operators
can dial budgets per agent — verbose-turn threads get more context;
FAQ agents save tokens.

Coverage:
* Pure model: new fields default None, validation bounds, is_default()
* RetrievalConfig validation (ge/le)
* Skill template: multi_hop_max_total_chunks plumbs through to
  kb_search when set
* Messages endpoint: history_turns + history_char_budget override
  the runtime defaults when the agent's bundle declares them
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import (
    AgentSpec,
    JobStatus,
    Metrics,
    RetrievalConfig,
    RunRecord,
    TokenUsage,
)
from movate.runtime import build_app
from movate.runtime.registry import scan_agents
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Pure model — new fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_new_overrides_default_to_none() -> None:
    cfg = RetrievalConfig()
    assert cfg.multi_hop_max_total_chunks is None
    assert cfg.history_turns is None
    assert cfg.history_char_budget is None
    # All-None = still default.
    assert cfg.is_default()


@pytest.mark.unit
def test_setting_any_override_unsets_default() -> None:
    assert not RetrievalConfig(history_turns=5).is_default()
    assert not RetrievalConfig(multi_hop_max_total_chunks=10).is_default()
    assert not RetrievalConfig(history_char_budget=20_000).is_default()


@pytest.mark.unit
def test_multi_hop_chunks_validation_bounds() -> None:
    RetrievalConfig(multi_hop_max_total_chunks=1)
    RetrievalConfig(multi_hop_max_total_chunks=30)
    with pytest.raises(ValidationError):
        RetrievalConfig(multi_hop_max_total_chunks=0)
    with pytest.raises(ValidationError):
        RetrievalConfig(multi_hop_max_total_chunks=31)


@pytest.mark.unit
def test_history_turns_validation_bounds() -> None:
    RetrievalConfig(history_turns=1)
    RetrievalConfig(history_turns=100)
    with pytest.raises(ValidationError):
        RetrievalConfig(history_turns=0)
    with pytest.raises(ValidationError):
        RetrievalConfig(history_turns=101)


@pytest.mark.unit
def test_history_char_budget_validation_bounds() -> None:
    RetrievalConfig(history_char_budget=1000)
    RetrievalConfig(history_char_budget=200_000)
    with pytest.raises(ValidationError):
        RetrievalConfig(history_char_budget=999)
    with pytest.raises(ValidationError):
        RetrievalConfig(history_char_budget=200_001)


# ---------------------------------------------------------------------------
# AgentSpec parses agent.yaml with the new overrides
# ---------------------------------------------------------------------------


_BASE_YAML = """\
api_version: movate/v1
kind: Agent
name: tuned
version: 0.1.0
description: Test
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
retrieval:
  multi_hop_max_total_chunks: 25
  history_turns: 5
  history_char_budget: 5000
"""


@pytest.mark.unit
def test_agent_yaml_parses_new_overrides() -> None:
    data = yaml.safe_load(_BASE_YAML)
    spec = AgentSpec.model_validate(data)
    assert spec.retrieval.multi_hop_max_total_chunks == 25
    assert spec.retrieval.history_turns == 5
    assert spec.retrieval.history_char_budget == 5000


# ---------------------------------------------------------------------------
# Skill template — multi_hop_max_total_chunks plumbs to kb_search
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_skill_passes_multi_hop_chunks_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the agent's retrieval config sets multi_hop_max_total_chunks,
    the skill plumbs it as the corresponding kwarg to kb_search."""
    from movate.core.skill_backend.base import SkillExecutionContext  # noqa: PLC0415
    from movate.kb import search as search_mod  # noqa: PLC0415
    from movate.templates.skill_kb_vector_lookup import impl  # noqa: PLC0415

    captured_kwargs: dict = {}

    async def fake_search(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(search_mod, "search", fake_search)

    ctx = SkillExecutionContext(
        tenant_id="t1",
        agent_name="rag-qa",
        storage=object(),
        retrieval=RetrievalConfig(multi_hop=2, multi_hop_max_total_chunks=25),
    )
    await impl.run({"question": "x"}, ctx=ctx)
    assert captured_kwargs["multi_hop_max_total_chunks"] == 25
    assert captured_kwargs["multi_hop"] == 2


@pytest.mark.unit
async def test_skill_omits_multi_hop_chunks_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the agent doesn't override, the kwarg isn't passed — keeps
    kb_search's process-default behavior."""
    from movate.core.skill_backend.base import SkillExecutionContext  # noqa: PLC0415
    from movate.kb import search as search_mod  # noqa: PLC0415
    from movate.templates.skill_kb_vector_lookup import impl  # noqa: PLC0415

    captured_kwargs: dict = {}

    async def fake_search(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(search_mod, "search", fake_search)

    ctx = SkillExecutionContext(
        tenant_id="t1",
        agent_name="rag-qa",
        storage=object(),
        retrieval=RetrievalConfig(multi_hop=2),
    )
    await impl.run({"question": "x"}, ctx=ctx)
    assert "multi_hop_max_total_chunks" not in captured_kwargs


# ---------------------------------------------------------------------------
# Messages endpoint — per-agent history budget overrides
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    """Scaffold a minimal agent with a tuned retrieval block on disk
    so the registry picks it up via scan_agents."""
    agents = tmp_path / "agents"
    demo = agents / "tuned"
    demo.mkdir(parents=True)
    (demo / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: tuned\n"
        "version: 0.1.0\n"
        "description: Tuned-budget demo\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n"
        "retrieval:\n"
        "  history_turns: 2\n"
        "  history_char_budget: 1000\n",
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
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    agents = scan_agents(agents_path)
    return TestClient(build_app(storage, agents=agents, agents_path=agents_path))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="budget-tests")
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _seed_runs(storage: InMemoryStorage, *, thread_id: str, tenant_id: str, n: int) -> None:
    """Seed N small runs in chronological order, all linked to the thread."""
    now = datetime.now(UTC)
    loop = asyncio.new_event_loop()
    try:
        for i in range(n):
            run = RunRecord(
                run_id=f"r{i}",
                job_id=f"j{i}",
                tenant_id=tenant_id,
                agent="tuned",
                agent_version="0.1.0",
                prompt_hash="h",
                provider="openai/gpt-4o-mini-2024-07-18",
                provider_version="1.0",
                pricing_version="2024-01-01",
                status=JobStatus.SUCCESS,
                input={"q": f"q{i}"},
                output={"a": f"a{i}"},
                metrics=Metrics(
                    latency_ms=50, cost_usd=0.001, tokens=TokenUsage(input=5, output=5)
                ),
                created_at=now + timedelta(seconds=i),
                thread_id=thread_id,
            )
            loop.run_until_complete(storage.save_run(run))
    finally:
        loop.close()


@pytest.mark.integration
def test_messages_endpoint_applies_agent_history_turns_override(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Agent declares history_turns=2 → only the most recent 2 turns
    appear in conversation_history even though 5 exist."""
    r = client.post("/api/v1/threads", json={"agent": "tuned"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    tenant_id = storage.conversation_threads[0].tenant_id
    _seed_runs(storage, thread_id=thread_id, tenant_id=tenant_id, n=5)
    r = client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "new"}},
        headers=auth_header,
    )
    assert r.status_code == 202
    history = storage.jobs[-1].input["conversation_history"]
    # 5 seeded; agent's history_turns=2 → only 2 fetched.
    assert len(history) == 2
    # The 2 most recent (chronological order).
    assert history[-1]["input"]["q"] == "q4"


@pytest.mark.integration
def test_messages_endpoint_applies_char_budget_override(
    client: TestClient,
    auth_header: dict[str, str],
    storage: InMemoryStorage,
) -> None:
    """Agent declares history_char_budget=1000. Inject one huge prior
    turn (~5000 chars) — only the most-recent fits."""
    r = client.post("/api/v1/threads", json={"agent": "tuned"}, headers=auth_header)
    thread_id = r.json()["thread_id"]
    tenant_id = storage.conversation_threads[0].tenant_id
    # 2 turns, each ~2500 chars. Total exceeds 1000-char budget.
    now = datetime.now(UTC)
    loop = asyncio.new_event_loop()
    try:
        for i in range(2):
            run = RunRecord(
                run_id=f"r{i}",
                job_id=f"j{i}",
                tenant_id=tenant_id,
                agent="tuned",
                agent_version="0.1.0",
                prompt_hash="h",
                provider="openai/gpt-4o-mini-2024-07-18",
                provider_version="1.0",
                pricing_version="2024-01-01",
                status=JobStatus.SUCCESS,
                input={"q": "x" * 2500},
                output={"a": "y"},
                metrics=Metrics(
                    latency_ms=50, cost_usd=0.001, tokens=TokenUsage(input=5, output=5)
                ),
                created_at=now + timedelta(seconds=i),
                thread_id=thread_id,
            )
            loop.run_until_complete(storage.save_run(run))
    finally:
        loop.close()

    client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"input": {"q": "new"}},
        headers=auth_header,
    )
    history = storage.jobs[-1].input["conversation_history"]
    # Most-recent turn kept (budget = 1000, single 2500-char turn
    # overflows — but the single-overflow rule keeps it).
    assert len(history) == 1
