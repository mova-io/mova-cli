"""Tests for the per-agent retrieval config (PR-I).

Covers:

* ``RetrievalConfig`` model — validation rules + ``is_default()``.
* ``AgentSpec`` integration — agent.yaml's ``retrieval:`` block
  parses into the config; absent block defaults to all-off.
* End-to-end through the ``kb-vector-lookup`` skill — the skill
  reads the config off ``SkillExecutionContext.retrieval`` and
  passes the right kwargs into ``kb_search``.

The kb_search call is mocked at the import boundary so the skill
test doesn't need a real KB / storage / embedding API key.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

import movate.kb.search as kb_search_mod
from movate.core.models import AgentSpec, RetrievalConfig
from movate.core.skill_backend.base import SkillExecutionContext

# Import the skill impl by its template path. The template files live
# under src/movate/templates/skill_kb_vector_lookup/impl.py; we import
# them as a regular Python module so the test exercises the same code
# the wizard scaffolds into operator projects.
from movate.templates.skill_kb_vector_lookup import impl as kb_lookup_skill

# ---------------------------------------------------------------------------
# RetrievalConfig — pure model tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retrieval_config_defaults_all_off() -> None:
    """Default-constructed RetrievalConfig has all retrieval stages
    disabled — preserves pre-PR-I vector-only behavior for agents
    that don't opt in."""
    cfg = RetrievalConfig()
    assert cfg.hybrid is False
    assert cfg.rewrite == 0
    assert cfg.rerank is False
    assert cfg.multi_hop == 0
    assert cfg.is_default() is True


@pytest.mark.unit
def test_retrieval_config_is_default_when_any_flag_changed() -> None:
    """is_default() returns False as soon as any field deviates —
    used by the skill to skip kwargs entirely on the default path."""
    assert not RetrievalConfig(hybrid=True).is_default()
    assert not RetrievalConfig(rewrite=1).is_default()
    assert not RetrievalConfig(rerank=True).is_default()
    assert not RetrievalConfig(multi_hop=1).is_default()


@pytest.mark.unit
def test_retrieval_config_rewrite_range_validated() -> None:
    """rewrite is clamped to [0, 8] at validation time — Pydantic
    rejects out-of-range values before the skill even sees them."""
    RetrievalConfig(rewrite=0)
    RetrievalConfig(rewrite=8)
    with pytest.raises(ValidationError):
        RetrievalConfig(rewrite=-1)
    with pytest.raises(ValidationError):
        RetrievalConfig(rewrite=9)


@pytest.mark.unit
def test_retrieval_config_multi_hop_range_validated() -> None:
    """multi_hop is clamped to [0, 5] at validation time."""
    RetrievalConfig(multi_hop=0)
    RetrievalConfig(multi_hop=5)
    with pytest.raises(ValidationError):
        RetrievalConfig(multi_hop=-1)
    with pytest.raises(ValidationError):
        RetrievalConfig(multi_hop=6)


@pytest.mark.unit
def test_retrieval_config_extra_fields_forbidden() -> None:
    """``extra="forbid"`` so a typo'd flag in agent.yaml gets caught
    at load time, not silently ignored."""
    with pytest.raises(ValidationError):
        RetrievalConfig(unknown_flag=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# AgentSpec integration — agent.yaml parses correctly
# ---------------------------------------------------------------------------


_BASE_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: refund-helper
version: 0.1.0
description: Test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""


@pytest.mark.unit
def test_agent_spec_defaults_retrieval_when_block_absent() -> None:
    """An agent.yaml WITHOUT a retrieval: block loads with the
    default config (all flags off). Back-compat for every existing
    operator project."""
    data = yaml.safe_load(_BASE_AGENT_YAML)
    spec = AgentSpec.model_validate(data)
    assert isinstance(spec.retrieval, RetrievalConfig)
    assert spec.retrieval.is_default()


@pytest.mark.unit
def test_agent_spec_parses_retrieval_block() -> None:
    """An agent.yaml WITH a retrieval: block parses each field into
    RetrievalConfig. The operator-tuned settings make it through."""
    yaml_with_block = (
        _BASE_AGENT_YAML
        + "retrieval:\n"
        + "  hybrid: true\n"
        + "  rewrite: 3\n"
        + "  rerank: true\n"
        + "  multi_hop: 2\n"
    )
    data = yaml.safe_load(yaml_with_block)
    spec = AgentSpec.model_validate(data)
    assert spec.retrieval.hybrid is True
    assert spec.retrieval.rewrite == 3
    assert spec.retrieval.rerank is True
    assert spec.retrieval.multi_hop == 2
    assert not spec.retrieval.is_default()


@pytest.mark.unit
def test_agent_spec_partial_retrieval_block_uses_field_defaults() -> None:
    """An agent.yaml with only SOME retrieval fields set keeps the
    others at their defaults — operator can opt into one stage at
    a time."""
    yaml_partial = _BASE_AGENT_YAML + "retrieval:\n  hybrid: true\n"
    data = yaml.safe_load(yaml_partial)
    spec = AgentSpec.model_validate(data)
    assert spec.retrieval.hybrid is True
    assert spec.retrieval.rewrite == 0
    assert spec.retrieval.rerank is False
    assert spec.retrieval.multi_hop == 0


# ---------------------------------------------------------------------------
# Skill template — reads retrieval off the context, plumbs to kb_search
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_storage() -> Any:
    """A minimal storage stand-in; the skill only needs an object
    reference to pass to kb_search (which we mock)."""
    return object()


@pytest.fixture
def mock_kb_search(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch kb_search so the skill's actual call shape is captured
    without a real KB / storage / embedding round trip."""
    mock = AsyncMock(return_value=[])
    # The skill imports kb_search lazily; patch at the module's
    # source so the lazy import sees the mock.
    monkeypatch.setattr(kb_search_mod, "search", mock)
    return mock


@pytest.mark.unit
async def test_skill_uses_default_kb_search_kwargs_when_no_retrieval(
    stub_storage: Any, mock_kb_search: AsyncMock
) -> None:
    """When ctx.retrieval is None (pre-PR-I path, or operator without
    a retrieval: block), the skill calls kb_search with no retrieval
    kwargs — same as before this PR."""
    ctx = SkillExecutionContext(
        agent_name="rag-qa",
        tenant_id="t1",
        storage=stub_storage,
        retrieval=None,
    )
    await kb_lookup_skill.run({"question": "test", "k": 3}, ctx=ctx)
    assert mock_kb_search.call_count == 1
    kwargs = mock_kb_search.call_args.kwargs
    # No retrieval-stage kwargs passed — kb_search applies its own
    # vector-only defaults.
    assert "hybrid" not in kwargs
    assert "rewrite_variants" not in kwargs
    assert "rerank" not in kwargs
    assert "multi_hop" not in kwargs
    # Standard kwargs still pass through.
    assert kwargs["agent"] == "rag-qa"
    assert kwargs["tenant_id"] == "t1"
    assert kwargs["limit"] == 3


@pytest.mark.unit
async def test_skill_plumbs_retrieval_config_to_kb_search(
    stub_storage: Any, mock_kb_search: AsyncMock
) -> None:
    """When ctx.retrieval is a RetrievalConfig with flags set, the
    skill maps each field to the right kb_search kwarg."""
    ctx = SkillExecutionContext(
        agent_name="rag-qa",
        tenant_id="t1",
        storage=stub_storage,
        retrieval=RetrievalConfig(hybrid=True, rewrite=3, rerank=True, multi_hop=2),
    )
    await kb_lookup_skill.run({"question": "test"}, ctx=ctx)
    kwargs = mock_kb_search.call_args.kwargs
    assert kwargs["hybrid"] is True
    assert kwargs["rewrite_variants"] == 3
    assert kwargs["rerank"] is True
    assert kwargs["multi_hop"] == 2


@pytest.mark.unit
async def test_skill_accepts_duck_typed_retrieval_object(
    stub_storage: Any, mock_kb_search: AsyncMock
) -> None:
    """The skill reads fields via getattr — any object exposing the
    right attribute names works. Loose coupling so future config
    sources (e.g. environment overrides) slot in without import gymnastics."""

    class _DuckCfg:
        hybrid = True
        rewrite = 0
        rerank = False
        multi_hop = 0

    ctx = SkillExecutionContext(
        agent_name="rag-qa",
        tenant_id="t1",
        storage=stub_storage,
        retrieval=_DuckCfg(),
    )
    await kb_lookup_skill.run({"question": "test"}, ctx=ctx)
    kwargs = mock_kb_search.call_args.kwargs
    assert kwargs["hybrid"] is True
    assert kwargs["rewrite_variants"] == 0


@pytest.mark.unit
async def test_skill_no_ctx_falls_back_to_fresh_storage(
    monkeypatch: pytest.MonkeyPatch, mock_kb_search: AsyncMock
) -> None:
    """CLI testing path: ctx=None → skill builds its own storage
    + uses no retrieval kwargs. Pre-existing behavior; PR-I doesn't
    regress it."""
    init_calls = 0

    class _FakeStorage:
        async def init(self) -> None:
            nonlocal init_calls
            init_calls += 1

    def _make_storage() -> _FakeStorage:
        return _FakeStorage()

    monkeypatch.setattr("movate.storage.build_storage", _make_storage)
    await kb_lookup_skill.run({"question": "test"})
    # build_storage().init() called once.
    assert init_calls == 1
    # kb_search called; no retrieval kwargs.
    kwargs = mock_kb_search.call_args.kwargs
    assert "hybrid" not in kwargs


# ---------------------------------------------------------------------------
# Executor integration — context carries the agent's retrieval config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skill_execution_context_carries_retrieval_field() -> None:
    """Sanity: the dataclass exposes the new fields with sensible
    defaults so the existing executor + CLI call sites that don't
    set them keep working."""
    ctx = SkillExecutionContext()
    assert ctx.agent_name == ""
    assert ctx.storage is None
    assert ctx.retrieval is None
    # Constructor accepts each field explicitly too.
    ctx2 = SkillExecutionContext(
        agent_name="foo",
        storage="stub",
        retrieval=RetrievalConfig(hybrid=True),
    )
    assert ctx2.agent_name == "foo"
    assert ctx2.storage == "stub"
    assert isinstance(ctx2.retrieval, RetrievalConfig)
    assert ctx2.retrieval.hybrid is True
