"""Conformance tests for ``movate.testing`` — what consumers will see.

These tests pretend to be a downstream agent author. If we ever break the
public shape (rename a fixture, change a double's protocol), this file is
the canary.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

# This file activates the fixture plugin the same way a consumer would.
pytest_plugins = ["movate.testing.fixtures"]

from movate.core.loader import load_agent  # noqa: E402
from movate.core.models import (  # noqa: E402
    JobStatus,
    Metrics,
    RunRecord,
    RunRequest,
    TokenUsage,
)
from movate.providers.base import (  # noqa: E402
    CompletionRequest,
    Message,
)
from movate.testing import (  # noqa: E402
    InMemoryStorage,
    JudgeStubProvider,
    MockProvider,
    NullTracer,
    build_test_executor,
    scaffold_agent,
)


@pytest.mark.unit
def test_public_surface_is_importable() -> None:
    """Every name the package promises must round-trip via attr access."""
    mod = importlib.import_module("movate.testing")
    expected = {
        "InMemoryStorage",
        "JudgeStubProvider",
        "MockProvider",
        "NullTracer",
        "build_test_executor",
        "scaffold_agent",
    }
    assert expected.issubset(set(mod.__all__))
    for name in expected:
        assert getattr(mod, name) is not None


# ---------------------------------------------------------------------------
# scaffold_agent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scaffold_agent_creates_loadable_bundle(tmp_path: pytest.TempPathFactory) -> None:
    dst = tmp_path / "demo"  # type: ignore[operator]
    scaffold_agent(dst, name="my-agent")
    bundle = load_agent(dst)
    assert bundle.spec.name == "my-agent"
    assert bundle.spec.api_version == "movate/v1"
    # Template ships with two eval cases.
    assert (dst / "evals" / "dataset.jsonl").exists()


@pytest.mark.unit
def test_scaffold_agent_rejects_existing_dir(tmp_path: pytest.TempPathFactory) -> None:
    dst = tmp_path / "demo"  # type: ignore[operator]
    scaffold_agent(dst, name="first")
    with pytest.raises(FileExistsError):
        scaffold_agent(dst, name="second")


# ---------------------------------------------------------------------------
# build_test_executor
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_build_test_executor_default_uses_mock(
    tmp_path: pytest.TempPathFactory,
) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo")  # type: ignore[operator]
    bundle = load_agent(agent_dir)

    executor, provider, storage, tracer = build_test_executor(response='{"message": "ok"}')
    assert isinstance(provider, MockProvider)
    assert isinstance(storage, InMemoryStorage)
    assert isinstance(tracer, NullTracer)

    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    assert response.data == {"message": "ok"}
    assert len(storage.runs) == 1
    assert storage.runs[0].status is JobStatus.SUCCESS


@pytest.mark.unit
async def test_build_test_executor_accepts_custom_provider(
    tmp_path: pytest.TempPathFactory,
) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo")  # type: ignore[operator]
    bundle = load_agent(agent_dir)

    custom = JudgeStubProvider(agent_response='{"message": "yo"}', judge_score=0.7)
    executor, provider, _, _ = build_test_executor(provider=custom)
    assert provider is custom

    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.data == {"message": "yo"}


# ---------------------------------------------------------------------------
# InMemoryStorage filters
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_in_memory_storage_filters_by_agent_and_tenant() -> None:
    s = InMemoryStorage()

    def _r(agent: str, tenant: str) -> RunRecord:
        return RunRecord(
            run_id=f"{agent}-{tenant}",
            job_id="j",
            tenant_id=tenant,
            agent=agent,
            agent_version="0.1.0",
            prompt_hash="h",
            provider="mock/x",
            provider_version="0",
            pricing_version="0",
            status=JobStatus.SUCCESS,
            input={},
            output={},
            metrics=Metrics(tokens=TokenUsage()),
            created_at=datetime.now(UTC),
        )

    await s.save_run(_r("a", "t1"))
    await s.save_run(_r("a", "t2"))
    await s.save_run(_r("b", "t1"))

    assert len(await s.list_runs(agent="a")) == 2
    assert len(await s.list_runs(tenant_id="t1")) == 2
    assert len(await s.list_runs(agent="a", tenant_id="t2")) == 1


# ---------------------------------------------------------------------------
# JudgeStubProvider behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_judge_stub_returns_agent_response_for_normal_prompt() -> None:
    p = JudgeStubProvider(agent_response='{"message": "hi"}', judge_score=0.5)
    resp = await p.complete(
        CompletionRequest(
            provider="openai/x",
            messages=[Message(role="user", content="please answer")],
        )
    )
    assert resp.text == '{"message": "hi"}'
    assert p.judge_prompts == []
    assert p.calls == ["openai/x"]


@pytest.mark.unit
async def test_judge_stub_returns_score_for_judge_prompt() -> None:
    p = JudgeStubProvider(agent_response='{"message": "hi"}', judge_score=0.9)
    resp = await p.complete(
        CompletionRequest(
            provider="anthropic/judge",
            messages=[Message(role="user", content="...\nRubric: be strict\n...")],
        )
    )
    assert "0.9" in resp.text
    assert "score" in resp.text
    assert len(p.judge_prompts) == 1


# ---------------------------------------------------------------------------
# Pytest fixtures (auto-discovered via pytest_plugins above)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fixture_mock_provider(mock_provider: MockProvider) -> None:
    assert isinstance(mock_provider, MockProvider)


@pytest.mark.unit
async def test_fixture_in_memory_storage(in_memory_storage: InMemoryStorage) -> None:
    # Already init()'d; calling save_run should not raise.
    assert isinstance(in_memory_storage, InMemoryStorage)
    assert in_memory_storage.runs == []


@pytest.mark.unit
def test_fixture_null_tracer(null_tracer: NullTracer) -> None:
    assert isinstance(null_tracer, NullTracer)


@pytest.mark.unit
def test_fixture_pricing(pricing) -> None:  # type: ignore[no-untyped-def]
    assert pricing.version
    assert "openai/gpt-4o-mini-2024-07-18" in pricing.models


@pytest.mark.unit
def test_fixture_temp_agent_dir(temp_agent_dir) -> None:  # type: ignore[no-untyped-def]
    bundle = load_agent(temp_agent_dir)
    assert bundle.spec.api_version == "movate/v1"


@pytest.mark.unit
async def test_fixture_build_executor(temp_agent_dir, build_executor) -> None:  # type: ignore[no-untyped-def]
    bundle = load_agent(temp_agent_dir)
    executor, _, storage, _ = build_executor(response='{"message": "fixture"}')
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"
    assert response.data == {"message": "fixture"}
    assert len(storage.runs) == 1
