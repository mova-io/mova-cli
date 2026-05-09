"""Markdown reporters: shape, edge cases, escaping."""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.bench import BenchEngine
from movate.core.eval import EvalEngine
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import JudgeConfig, JudgeMethod, ModelConfig
from movate.core.reporters import render_bench_markdown, render_eval_markdown
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import (
    InMemoryStorage,
    JudgeStubProvider,
    NullTracer,
    scaffold_agent,
)


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


# ---------------------------------------------------------------------------
# render_eval_markdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_eval_markdown_pass_verdict(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    bundle = load_agent(scaffold_agent(tmp_path / "demo", template="default"))
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    summary = await EvalEngine(executor=executor, provider=provider).run(bundle)

    md = render_eval_markdown(summary, gate=0.0)  # gate=0 → all cases pass
    assert "✅ PASS" in md
    assert "🟢" in md
    assert "movate eval — `demo`" in md
    assert "| Cases | 2 |" in md
    assert "Pass rate | 2/2 (100%)" in md
    assert "Per-case results (2)" in md
    assert md.endswith("\n") or "</details>" in md


@pytest.mark.unit
async def test_eval_markdown_fail_verdict(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    bundle = load_agent(scaffold_agent(tmp_path / "demo", template="default"))
    provider = MockProvider(response='{"message": "wrong"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    summary = await EvalEngine(executor=executor, provider=provider).run(bundle)

    md = render_eval_markdown(summary, gate=0.7)  # gate=0.7 → all cases fail (score 0)
    assert "❌ FAIL" in md
    assert "🔴" in md
    assert "Pass rate | 0/2 (0%)" in md
    # Each case row should carry the fail check
    assert md.count("| ❌ |") == 2


@pytest.mark.unit
async def test_eval_markdown_includes_judge_provider_when_present(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo", template="default")
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\nmodel:\n  provider: anthropic/claude-sonnet-4-6\nrubric: 'be strict'\n"
    )
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "x"}', judge_score=0.8)
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    summary = await EvalEngine(executor=executor, provider=provider).run(bundle)

    md = render_eval_markdown(summary, gate=0.7)
    assert "llm_judge" in md
    assert "anthropic/claude-sonnet-4-6" in md


@pytest.mark.unit
async def test_eval_markdown_with_empty_cases(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Dataset with zero rows → no per-case section, verdict still emitted."""
    agent_dir = scaffold_agent(tmp_path / "demo", template="default")
    (agent_dir / "evals" / "dataset.jsonl").write_text("")
    bundle = load_agent(agent_dir)
    provider = MockProvider()
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    summary = await EvalEngine(executor=executor, provider=provider).run(bundle)

    md = render_eval_markdown(summary, gate=0.7)
    # No cases → can't pass; renderer should fall through to FAIL.
    assert "❌ FAIL" in md
    assert "Per-case results" not in md
    assert "0/0 (0%)" in md


@pytest.mark.unit
async def test_eval_markdown_truncates_long_input(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo", template="default")
    long_text = "x" * 200
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "' + long_text + '"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "ok"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    summary = await EvalEngine(executor=executor, provider=provider).run(bundle)

    md = render_eval_markdown(summary, gate=0.7)
    # Truncation → ellipsis present, full long_text NOT present in output.
    assert "…" in md
    assert long_text not in md


@pytest.mark.unit
async def test_eval_markdown_escapes_pipe_in_input(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    """Pipe characters in input must be escaped or the markdown table breaks."""
    agent_dir = scaffold_agent(tmp_path / "demo", template="default")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "a|b|c"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "ok"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    summary = await EvalEngine(executor=executor, provider=provider).run(bundle)

    md = render_eval_markdown(summary, gate=0.7)
    case_lines = [line for line in md.splitlines() if line.startswith("| 1 |")]
    assert case_lines, "expected exactly one case row starting with '| 1 |'"
    # The case row should still parse as a valid markdown row: 7 cells (6 separators
    # plus the trailing one) — i.e. 6 internal pipes after escaping.
    # `| # | Score | Pass | Input | Rationale |` has 5 columns → 6 unescaped pipes
    # in a properly-formed row. Pipes inside the input should be escaped (`\|`).
    assert "\\|" in case_lines[0]


# ---------------------------------------------------------------------------
# render_bench_markdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_bench_markdown_no_judge(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    bundle = load_agent(scaffold_agent(tmp_path / "demo", template="default"))
    provider = MockProvider(response='{"message": "ok"}')
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    engine = BenchEngine(executor=executor, provider=provider, runs_per_model=1)
    summary = await engine.run(
        bundle,
        input_payload={"text": "hi"},
        providers=[
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",
        ],
    )

    md = render_bench_markdown(summary)
    assert "movate bench — `demo`" in md
    assert "openai/gpt-4o-mini-2024-07-18" in md
    assert "anthropic/claude-haiku-4-5-20251001" in md
    # No judge → no "Score" column header
    header = next(line for line in md.splitlines() if line.startswith("| Model"))
    assert "Score" not in header


@pytest.mark.unit
async def test_bench_markdown_with_judge_includes_skip_note(
    tmp_path: Path, pricing: PricingTable, storage: InMemoryStorage, tracer: NullTracer
) -> None:
    bundle = load_agent(scaffold_agent(tmp_path / "demo", template="default"))
    provider = JudgeStubProvider(agent_response='{"message": "x"}', judge_score=0.9)
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    judge = JudgeConfig(
        method=JudgeMethod.LLM_JUDGE,
        model=ModelConfig(provider="anthropic/claude-sonnet-4-6"),
        rubric="be strict",
    )
    engine = BenchEngine(executor=executor, provider=provider, runs_per_model=1, judge=judge)
    summary = await engine.run(
        bundle,
        input_payload={"text": "hi"},
        providers=[
            "openai/gpt-4o-mini-2024-07-18",
            "anthropic/claude-haiku-4-5-20251001",  # same family as judge → skip
        ],
    )

    md = render_bench_markdown(summary)
    header = next(line for line in md.splitlines() if line.startswith("| Model"))
    assert "Score" in header
    assert "_skipped_" in md
    assert "judge skipped on same-family rows" in md
