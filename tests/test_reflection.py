"""Reflection pattern (Phase J-1) — judge-in-the-loop self-critique.

Three layers of coverage:

1. **Schema** — :class:`ReflectionConfig` validates cross-field
   constraints: enabled requires judge_model + rubric;
   require_cross_family blocks same-family judge configs.
2. **Module** — :func:`call_judge` parses verdicts permissively (raw
   JSON, code fences, malformed → parse_error), and
   :func:`build_revision_prompt` constructs the correction directive.
3. **Executor integration** — when a bundle has reflection enabled,
   the executor calls the judge after schema validation; on revise it
   re-prompts and re-validates; max_iterations caps the loop.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import (
    ReflectionConfig,
    RunRequest,
)
from movate.core.reflection import (
    _parse_verdict,
    build_revision_prompt,
    call_judge,
)
from movate.providers.base import CompletionResponse, TokenUsage
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

# ---------------------------------------------------------------------------
# Schema: ReflectionConfig + AgentSpec cross-field validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReflectionConfigSchema:
    def test_disabled_default_is_permissive(self) -> None:
        cfg = ReflectionConfig()
        assert cfg.enabled is False
        assert cfg.judge_model == ""
        assert cfg.rubric == ""
        assert cfg.max_iterations == 1
        assert cfg.require_cross_family is True

    def test_enabled_without_judge_model_rejected_at_agent_spec_load(self, tmp_path: Path) -> None:
        """`mdk validate` catches this at parse time — friendlier than
        a runtime crash."""
        agent_dir = tmp_path / "broken"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\n"
            "kind: Agent\n"
            "name: broken\n"
            "version: 0.1.0\n"
            "model:\n"
            "  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n"
            "  input: { q: string }\n"
            "  output: { a: string }\n"
            "reflection:\n"
            "  enabled: true\n"
            # judge_model missing
            "  rubric: 'something'\n"
        )
        (agent_dir / "prompt.md").write_text("{{ input.q }}")
        with pytest.raises(Exception, match="judge_model"):
            load_agent(agent_dir)

    def test_enabled_with_same_family_judge_rejected(self, tmp_path: Path) -> None:
        """openai-grading-openai is the sycophancy mistake. Caught at parse time."""
        agent_dir = tmp_path / "same-family"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\n"
            "kind: Agent\n"
            "name: same-family\n"
            "version: 0.1.0\n"
            "model:\n"
            "  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n"
            "  input: { q: string }\n"
            "  output: { a: string }\n"
            "reflection:\n"
            "  enabled: true\n"
            "  judge_model: openai/gpt-4-turbo-2024-04-09\n"
            "  rubric: 'something'\n"
        )
        (agent_dir / "prompt.md").write_text("{{ input.q }}")
        with pytest.raises(Exception, match="same provider family"):
            load_agent(agent_dir)

    def test_cross_family_judge_accepted(self, tmp_path: Path) -> None:
        """anthropic judging openai is the intended pattern."""
        agent_dir = tmp_path / "cross"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\n"
            "kind: Agent\n"
            "name: cross\n"
            "version: 0.1.0\n"
            "model:\n"
            "  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n"
            "  input: { q: string }\n"
            "  output: { a: string }\n"
            "reflection:\n"
            "  enabled: true\n"
            "  judge_model: anthropic/claude-haiku-4-5-20251001\n"
            "  rubric: 'something concrete'\n"
        )
        (agent_dir / "prompt.md").write_text("{{ input.q }}")
        bundle = load_agent(agent_dir)
        assert bundle.spec.reflection.enabled is True
        assert bundle.spec.reflection.judge_model == "anthropic/claude-haiku-4-5-20251001"

    def test_require_cross_family_false_allows_same_family(self, tmp_path: Path) -> None:
        """Escape hatch: operator can opt out for structured-output judging."""
        agent_dir = tmp_path / "opted-out"
        agent_dir.mkdir()
        (agent_dir / "agent.yaml").write_text(
            "api_version: movate/v1\n"
            "kind: Agent\n"
            "name: opted-out\n"
            "version: 0.1.0\n"
            "model:\n"
            "  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n"
            "  input: { q: string }\n"
            "  output: { a: string }\n"
            "reflection:\n"
            "  enabled: true\n"
            "  judge_model: openai/gpt-4-turbo-2024-04-09\n"
            "  rubric: 'something'\n"
            "  require_cross_family: false\n"
        )
        (agent_dir / "prompt.md").write_text("{{ input.q }}")
        bundle = load_agent(agent_dir)
        assert bundle.spec.reflection.enabled is True


# ---------------------------------------------------------------------------
# Module: _parse_verdict + build_revision_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseVerdict:
    def test_accept_verdict(self) -> None:
        v, fb = _parse_verdict('{"verdict": "accept", "feedback": ""}')
        assert v == "accept"
        assert fb == ""

    def test_revise_verdict_with_feedback(self) -> None:
        v, fb = _parse_verdict('{"verdict": "revise", "feedback": "SQL must be read-only"}')
        assert v == "revise"
        assert "read-only" in fb

    def test_strips_markdown_code_fences(self) -> None:
        """Judges sometimes ignore the instruction and wrap in ```json — accept it."""
        v, _fb = _parse_verdict('```json\n{"verdict": "accept", "feedback": ""}\n```')
        assert v == "accept"

    def test_malformed_json_returns_parse_error(self) -> None:
        v, fb = _parse_verdict("this isn't JSON")
        assert v == "parse_error"
        assert fb == ""

    def test_unknown_verdict_returns_parse_error(self) -> None:
        """A judge that invents a third verdict value falls into parse_error."""
        v, _fb = _parse_verdict('{"verdict": "maybe", "feedback": "..."}')
        assert v == "parse_error"

    def test_non_dict_response_returns_parse_error(self) -> None:
        v, _fb = _parse_verdict('["accept"]')
        assert v == "parse_error"


@pytest.mark.unit
class TestBuildRevisionPrompt:
    def test_preserves_original_message(self) -> None:
        out = build_revision_prompt("Translate to French", "do not capitalise nouns")
        assert "Translate to French" in out

    def test_includes_feedback(self) -> None:
        out = build_revision_prompt("...", "missing the foo field")
        assert "missing the foo field" in out

    def test_instructs_correction_only_output(self) -> None:
        """The model must produce ONE corrected JSON, no apology."""
        out = build_revision_prompt("anything", "any feedback")
        assert "corrected" in out.lower()
        assert "no apology" in out.lower() or "no prose" in out.lower()


# ---------------------------------------------------------------------------
# Module: call_judge
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_judge_accept_returns_accept_verdict() -> None:
    """A judge that says accept results in an accept verdict + cost tracking."""
    judge_provider = MockProvider(response='{"verdict": "accept", "feedback": ""}')
    pricing = load_pricing()
    cfg = ReflectionConfig(
        enabled=True,
        judge_model="anthropic/claude-haiku-4-5-20251001",
        rubric="must be polite",
    )
    verdict = await call_judge(
        config=cfg,
        output_text='{"answer": "hello"}',
        judge_provider=judge_provider,
        pricing_lookup=pricing,
    )
    assert verdict.verdict == "accept"
    assert verdict.feedback == ""


@pytest.mark.unit
async def test_call_judge_revise_returns_feedback() -> None:
    judge_provider = MockProvider(response='{"verdict": "revise", "feedback": "answer was rude"}')
    cfg = ReflectionConfig(
        enabled=True,
        judge_model="anthropic/claude-haiku-4-5-20251001",
        rubric="must be polite",
    )
    verdict = await call_judge(
        config=cfg,
        output_text='{"answer": "go away"}',
        judge_provider=judge_provider,
        pricing_lookup=load_pricing(),
    )
    assert verdict.verdict == "revise"
    assert "rude" in verdict.feedback


@pytest.mark.unit
async def test_call_judge_malformed_returns_parse_error() -> None:
    """A flaky judge surfaces as parse_error — the executor will soft-accept.

    Uses AsyncMock directly because :class:`MockProvider` validates
    that its scripted response is well-formed JSON at construction —
    we explicitly need to test a NON-JSON judge response here.
    """
    judge_provider = MockProvider(response='{"any": "valid"}')
    judge_provider.complete = AsyncMock(  # type: ignore[method-assign]
        return_value=CompletionResponse(
            text="this is not JSON at all",
            tokens=TokenUsage(input=10, output=5),
        )
    )
    cfg = ReflectionConfig(
        enabled=True,
        judge_model="anthropic/claude-haiku-4-5-20251001",
        rubric="any",
    )
    verdict = await call_judge(
        config=cfg,
        output_text='{"answer": "hi"}',
        judge_provider=judge_provider,
        pricing_lookup=load_pricing(),
    )
    assert verdict.verdict == "parse_error"


# ---------------------------------------------------------------------------
# Executor integration
# ---------------------------------------------------------------------------


def _build_agent_with_reflection(
    tmp_path: Path,
    *,
    rubric: str = "answer must not be empty",
    max_iterations: int = 1,
) -> Path:
    """Scaffold a default-template agent and tack reflection onto its yaml."""
    dst = tmp_path / "demo"
    scaffold_agent(dst, name="demo", template="default")
    yaml_path = dst / "agent.yaml"
    yaml_text = yaml_path.read_text()
    # Insert reflection block before the trailing newline.
    yaml_path.write_text(
        yaml_text.rstrip()
        + "\nreflection:\n"
        + "  enabled: true\n"
        + "  judge_model: anthropic/claude-haiku-4-5-20251001\n"
        + f"  rubric: '{rubric}'\n"
        + f"  max_iterations: {max_iterations}\n"
    )
    return dst


@pytest.mark.unit
async def test_executor_skips_reflection_when_disabled(tmp_path: Path) -> None:
    """Sanity: an agent without reflection.enabled never invokes the judge."""
    dst = tmp_path / "demo"
    scaffold_agent(dst, name="demo", template="default")
    bundle = load_agent(dst)
    assert bundle.spec.reflection.enabled is False

    provider = MockProvider(response='{"message": "hi"}')
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=provider,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hello"}))
    assert response.status == "success"
    assert response.data == {"message": "hi"}


@pytest.mark.unit
async def test_executor_reflection_accept_returns_original(tmp_path: Path) -> None:
    """Judge accepts → output unchanged + cost stays small (1 judge call)."""
    dst = _build_agent_with_reflection(tmp_path)
    bundle = load_agent(dst)

    # Tricky: agent + judge both route through the same MockProvider in
    # tests because the registry maps every runtime to the same mock.
    # Configure the mock to return TWO sequential responses: first the
    # agent's output, then the judge's accept. We do that by using
    # asyncMock on `complete`.
    agent_response = '{"message": "hello"}'
    judge_response = '{"verdict": "accept", "feedback": ""}'

    provider = MockProvider(response=agent_response)
    # Wire a side_effect so successive .complete() calls return the
    # agent's response first, then the judge's accept.
    complete_responses = [
        CompletionResponse(text=agent_response, tokens=TokenUsage(input=10, output=5)),
        CompletionResponse(text=judge_response, tokens=TokenUsage(input=20, output=8)),
    ]
    provider.complete = AsyncMock(side_effect=complete_responses)  # type: ignore[method-assign]

    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=provider,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    assert response.data == {"message": "hello"}
    # The judge was called exactly once (the agent once, the judge once).
    assert provider.complete.call_count == 2


@pytest.mark.unit
async def test_executor_reflection_revise_reprompts_and_uses_revision(tmp_path: Path) -> None:
    """max_iterations=2: judge rejects → re-prompt → judge accepts the revision."""
    dst = _build_agent_with_reflection(tmp_path, max_iterations=2)
    bundle = load_agent(dst)

    bad_response = '{"message": ""}'  # empty per rubric
    revise_verdict = '{"verdict": "revise", "feedback": "message must not be empty"}'
    revised_response = '{"message": "hello"}'
    accept_verdict = '{"verdict": "accept", "feedback": ""}'

    provider = MockProvider(response=bad_response)
    provider.complete = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            CompletionResponse(text=bad_response, tokens=TokenUsage(input=5, output=2)),
            CompletionResponse(text=revise_verdict, tokens=TokenUsage(input=20, output=10)),
            CompletionResponse(text=revised_response, tokens=TokenUsage(input=15, output=4)),
            CompletionResponse(text=accept_verdict, tokens=TokenUsage(input=20, output=5)),
        ]
    )

    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=provider,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    # The revised output wins.
    assert response.data == {"message": "hello"}
    # 4 calls: agent original + judge revise + agent revision + judge accept
    assert provider.complete.call_count == 4


@pytest.mark.unit
async def test_executor_reflection_max_iterations_one_does_not_reprompt(
    tmp_path: Path,
) -> None:
    """max_iterations=1 (the default) — judge runs but never re-prompts.

    Audit-only mode: the judge's verdict is logged but the original
    output is returned as-is even if the judge says revise.
    """
    dst = _build_agent_with_reflection(tmp_path, max_iterations=1)
    bundle = load_agent(dst)

    agent_response = '{"message": ""}'
    revise_verdict = '{"verdict": "revise", "feedback": "should not be empty"}'

    provider = MockProvider(response=agent_response)
    provider.complete = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            CompletionResponse(text=agent_response, tokens=TokenUsage(input=10, output=5)),
            CompletionResponse(text=revise_verdict, tokens=TokenUsage(input=20, output=10)),
        ]
    )

    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=provider,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    # Original output kept (no re-prompt fired).
    assert response.data == {"message": ""}
    assert provider.complete.call_count == 2  # agent + 1 judge only


@pytest.mark.unit
async def test_executor_reflection_parse_error_is_soft_accept(tmp_path: Path) -> None:
    """A judge that returns garbage doesn't block the run — we return the
    original output and log a warning. Flaky judge != failed run."""
    dst = _build_agent_with_reflection(tmp_path)
    bundle = load_agent(dst)

    agent_response = '{"message": "hi"}'
    garbage = "I refuse to follow instructions"

    provider = MockProvider(response=agent_response)
    provider.complete = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            CompletionResponse(text=agent_response, tokens=TokenUsage(input=10, output=5)),
            CompletionResponse(text=garbage, tokens=TokenUsage(input=15, output=8)),
        ]
    )

    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=provider,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    # Soft-accept: success, original output preserved.
    assert response.status == "success"
    assert response.data == {"message": "hi"}
