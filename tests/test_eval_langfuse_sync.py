"""End-to-end: an eval run wired to a stubbed Langfuse client pushes the
per-case + run-level scores and syncs the dataset (ADR 031 D1).

Hermetic — no real Langfuse: a fake v3 client (mirroring the SDK surface the
tracer calls) is injected into ``LangfuseTracer``, which is wired into the
``Executor`` the ``EvalEngine`` runs against, exactly as the local CLI runtime
would. The same edge helpers ``mdk eval`` calls (``push_eval_scores`` /
``sync_eval_dataset``) are then exercised against that tracer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from movate.core.eval import EvalEngine
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent
from movate.tracing.eval_sync import push_eval_scores, sync_eval_dataset
from movate.tracing.langfuse import LangfuseTracer

# Reuse the fake v3 client from the tracer unit tests.
from tests.test_tracing_langfuse import _FakeClient


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


def _two_case_bundle(tmp_path: Path) -> Path:
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hello"}, "expected": {"message": "Hello!"}}\n'
        '{"input": {"text": "bye"}, "expected": {"message": "Goodbye!"}}\n'
    )
    return agent_dir


@pytest.mark.unit
async def test_eval_pushes_per_case_and_run_level_scores(
    tmp_path: Path, pricing: PricingTable, storage
) -> None:
    agent_dir = _two_case_bundle(tmp_path)
    bundle = load_agent(agent_dir)
    fake = _FakeClient()
    tracer = LangfuseTracer(client=fake)
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    # Per-case accuracy scores were pushed during the run (existing behaviour).
    per_case = [s for s in fake.scores if s["name"] == "eval_accuracy"]
    assert len(per_case) == summary.sample_count

    # Now the run-level push the CLI/dispatch edges do.
    await push_eval_scores(tracer, summary, drift_deltas={"mean_score": -0.1})

    by_name = {s["name"]: s for s in fake.scores}
    assert by_name["eval_pass_rate"]["value"] == pytest.approx(summary.pass_rate)
    assert by_name["eval_mean_score"]["value"] == pytest.approx(summary.mean_score)
    assert by_name["eval_drift_mean_score"]["value"] == pytest.approx(-0.1)
    # The run-level scores landed on a real trace from this eval run.
    assert by_name["eval_pass_rate"]["trace_id"]


@pytest.mark.unit
async def test_eval_syncs_dataset(tmp_path: Path, pricing: PricingTable, storage) -> None:
    agent_dir = _two_case_bundle(tmp_path)
    bundle = load_agent(agent_dir)
    fake = _FakeClient()
    tracer = LangfuseTracer(client=fake)
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    cases = [cs.case for cs in summary.cases]
    synced = await sync_eval_dataset(tracer, agent=summary.agent, cases=cases)
    assert synced == 2
    assert fake.datasets == [
        {"name": "mdk-eval-demo", "description": "mdk eval dataset for agent 'demo'"}
    ]
    assert len(fake.dataset_items) == 2
    # Re-sync is idempotent (item ids stable → upsert, not duplicate).
    await sync_eval_dataset(tracer, agent=summary.agent, cases=cases)
    assert len(fake.dataset_items) == 2


@pytest.mark.unit
async def test_eval_noop_without_langfuse(tmp_path: Path, pricing: PricingTable, storage) -> None:
    """With a non-Langfuse tracer the edge helpers are silent no-ops — the
    eval runs unchanged and nothing is pushed."""
    agent_dir = _two_case_bundle(tmp_path)
    bundle = load_agent(agent_dir)
    tracer: Any = NullTracer()
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)

    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    # No exceptions, no scores/datasets (NullTracer has no extension methods).
    await push_eval_scores(tracer, summary)
    cases = [cs.case for cs in summary.cases]
    assert await sync_eval_dataset(tracer, agent=summary.agent, cases=cases) == 0
