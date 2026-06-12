"""Pricing-table model-name alias resolution (date-suffix normalization).

Regression coverage for a real production bug: the certification suite's
cost capability failed on the deployed Temporal worker with
``sum(cost_usd)=0.0 across 2 run fact(s)`` because the expense-approval
agents declare the undated alias ``openai/gpt-4o-mini`` while
``providers/pricing.yaml`` keys only the dated snapshot
``openai/gpt-4o-mini-2024-07-18``. ``PricingTable.cost_for`` did an exact
``dict.get`` and the executor's ``_turn_cost`` swallowed the ``KeyError``
into a silent $0 — on BOTH the native and Temporal paths (they share the
Executor); the Temporal cert run was simply where it was first measured.

These tests pin:

1. The concrete model strings the worker actually sees — the undated alias
   AND the date-suffixed variant — resolve to nonzero pricing against the
   PACKAGED table (so a future pricing.yaml edit can't silently regress).
2. The documented fallback order of :meth:`PricingTable.resolve_key`.
3. A genuinely unknown model still KeyErrors in ``cost_for`` and the
   executor records $0 *loudly* — a WARNING naming the model, never a
   silent zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import RunRequest, TokenUsage
from movate.providers.mock import MockProvider
from movate.providers.pricing import ModelPrice, PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

_TOKENS = TokenUsage(input=1000, output=1000)


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


# ---------------------------------------------------------------------------
# resolve_key / cost_for against the PACKAGED table — the worker-visible strings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_undated_alias_resolves_to_dated_entry(pricing: PricingTable) -> None:
    """``openai/gpt-4o-mini`` (the string in the expense-approval agent.yamls,
    passed through unchanged by LiteLLM's default ``pricing_key``) must bill
    at the dated snapshot's rate — this exact string produced the cert
    suite's $0 run facts."""
    assert pricing.resolve_key("openai/gpt-4o-mini") == "openai/gpt-4o-mini-2024-07-18"
    cost = pricing.cost_for(provider="openai/gpt-4o-mini", tokens=_TOKENS)
    assert cost > 0
    assert cost == pricing.cost_for(provider="openai/gpt-4o-mini-2024-07-18", tokens=_TOKENS)


@pytest.mark.unit
def test_dated_variant_still_resolves_exactly(pricing: PricingTable) -> None:
    """The date-suffixed form (what litellm may report as the concrete model)
    is the table's own key — exact match, nonzero."""
    assert pricing.resolve_key("openai/gpt-4o-mini-2024-07-18") == "openai/gpt-4o-mini-2024-07-18"
    assert pricing.cost_for(provider="openai/gpt-4o-mini-2024-07-18", tokens=_TOKENS) > 0


@pytest.mark.unit
def test_undated_anthropic_alias_resolves_compact_date_suffix(pricing: PricingTable) -> None:
    """Anthropic uses the compact ``-YYYYMMDD`` suffix shape — the alias
    bridge must handle both formats."""
    assert pricing.resolve_key("anthropic/claude-haiku-4-5") == (
        "anthropic/claude-haiku-4-5-20251001"
    )
    assert pricing.cost_for(provider="anthropic/claude-haiku-4-5", tokens=_TOKENS) > 0


@pytest.mark.unit
def test_newer_dated_snapshot_falls_back_to_dated_sibling(pricing: PricingTable) -> None:
    """A provider reporting a NEWER snapshot than the table carries falls
    back to the same base model's dated sibling instead of $0."""
    assert pricing.resolve_key("openai/gpt-4o-mini-2025-06-01") == ("openai/gpt-4o-mini-2024-07-18")
    assert pricing.cost_for(provider="openai/gpt-4o-mini-2025-06-01", tokens=_TOKENS) > 0


@pytest.mark.unit
def test_alias_never_cross_matches_a_longer_model_name(pricing: PricingTable) -> None:
    """``openai/gpt-4o`` must resolve to gpt-4o's dated entry, NOT
    gpt-4o-mini's — the suffix-strip comparison is on the full base name."""
    assert pricing.resolve_key("openai/gpt-4o") == "openai/gpt-4o-2024-08-06"


@pytest.mark.unit
def test_unknown_model_is_a_keyerror(pricing: PricingTable) -> None:
    assert pricing.resolve_key("openai/totally-unknown-model") is None
    with pytest.raises(KeyError):
        pricing.cost_for(provider="openai/totally-unknown-model", tokens=_TOKENS)


@pytest.mark.unit
def test_resolve_key_prefers_latest_dated_sibling() -> None:
    """When multiple dated snapshots of one base model exist, the undated
    alias bills at the LATEST one (documented fallback order)."""
    table = PricingTable(
        version="t",
        last_verified="2026-06-11",
        models={
            "openai/gpt-test-2024-01-01": ModelPrice(input_per_1k=1.0, output_per_1k=1.0),
            "openai/gpt-test-2025-01-01": ModelPrice(input_per_1k=2.0, output_per_1k=2.0),
        },
    )
    assert table.resolve_key("openai/gpt-test") == "openai/gpt-test-2025-01-01"


# ---------------------------------------------------------------------------
# Executor-level — the run record carries real cost / warns on a true miss
# ---------------------------------------------------------------------------


def _scaffold_with_provider(dst: Path, provider: str) -> Path:
    bundle_dir = scaffold_agent(dst, name="cost-demo")
    yaml_path = bundle_dir / "agent.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec["model"]["provider"] = provider
    yaml_path.write_text(yaml.safe_dump(spec))
    return bundle_dir


@pytest.mark.unit
async def test_executor_meters_cost_for_undated_alias(
    tmp_path: Path, pricing: PricingTable
) -> None:
    """End-to-end through the Executor (the construction both
    ``runtime.dispatch`` and ``temporal_activities._executor_for`` mirror):
    an agent declaring ``openai/gpt-4o-mini`` records cost_usd > 0."""
    bundle = load_agent(_scaffold_with_provider(tmp_path / "alias-demo", "openai/gpt-4o-mini"))
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=MockProvider(response='{"message": "hello"}'),
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )
    response = await executor.execute(bundle, RunRequest(agent="cost-demo", input={"text": "hi"}))
    assert response.status == "success"
    assert response.metrics.cost_usd > 0


@pytest.mark.unit
async def test_executor_warns_loudly_on_genuine_pricing_miss(
    tmp_path: Path,
    pricing: PricingTable,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model with NO table entry (even after alias normalization) still
    records $0 — but emits a WARNING naming the model. Never a silent zero."""
    bundle = load_agent(
        _scaffold_with_provider(tmp_path / "miss-demo", "openai/totally-unknown-model")
    )
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=MockProvider(response='{"message": "hello"}'),
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )
    with caplog.at_level("WARNING", logger="movate.core.executor"):
        response = await executor.execute(
            bundle, RunRequest(agent="cost-demo", input={"text": "hi"})
        )
    assert response.status == "success"
    assert response.metrics.cost_usd == 0.0
    warning = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "totally-unknown-model" in r.getMessage()
    ]
    assert warning, "pricing miss must WARN with the model name (never a silent $0)"
    assert "no pricing entry" in warning[0].getMessage()
