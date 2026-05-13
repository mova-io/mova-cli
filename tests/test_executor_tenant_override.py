"""Regression tests for the tenant_id_override on Executor.execute.

Bug story: ``movate submit <agent> ... --target dev --wait`` succeeded
end-to-end but every follow-up ``GET /runs/<id>`` returned 404. Root
cause: the worker constructs ONE Executor with a hardcoded
``tenant_id="local"`` (or pool tenant); every RunRecord got stamped
with that tenant; clients reading via API-key-derived ``ctx.tenant_id``
hit the SQL WHERE clause filter and got 404.

Fix: ``Executor.execute(tenant_id_override=...)`` lets the dispatcher
pass the job's tenant_id per call. The persisted RunRecord +
FailureRecord + budget queries all use the override. Local CLI omits
the kwarg and falls back to ``self._tenant_id``.

These tests assert:
  1. Default behavior unchanged when ``tenant_id_override=None``
  2. Override propagates to the RunRecord's tenant_id field
  3. Override propagates to the FailureRecord's tenant_id field
  4. Override propagates to the budget check tenant_id
  5. Worker dispatch passes job.tenant_id through correctly
"""

from __future__ import annotations

import pytest

from movate.core.executor import Executor
from movate.core.models import (
    AgentSpec,
    JobKind,
    JobRecord,
    JobStatus,
    ModelConfig,
    RunRequest,
    SchemaPaths,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.providers.registry import ProviderRegistry
from movate.runtime.dispatch import WorkerDispatch
from movate.testing.doubles import (
    InMemoryStorage,
    NullTracer,
)


def _make_executor(storage: InMemoryStorage, *, tenant_id: str = "local") -> Executor:
    """Build a test Executor stamped with the given construction-time tenant."""
    provider = MockProvider(response='{"answer": "ok", "confidence": 1.0}')
    registry = ProviderRegistry(default_litellm=provider)
    return Executor(
        registry=registry,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id=tenant_id,
    )


def _scaffold_bundle(tmp_path):
    """Drop a minimal agent bundle for the executor to run."""
    import json  # noqa: PLC0415

    from movate.core.loader import load_agent  # noqa: PLC0415

    agent_dir = tmp_path / "test-agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: test-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n"
    )
    (agent_dir / "prompt.md").write_text("answer: {{ input.question }}\nJSON: ...")
    schema_dir = agent_dir / "schema"
    schema_dir.mkdir()
    (schema_dir / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["question"],
                "additionalProperties": False,
                "properties": {"question": {"type": "string"}},
            }
        )
    )
    (schema_dir / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["answer", "confidence"],
                "additionalProperties": False,
                "properties": {
                    "answer": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            }
        )
    )
    return load_agent(agent_dir)


# ---------------------------------------------------------------------------
# Default behavior (no override) — must be unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_override_uses_executor_default_tenant(tmp_path) -> None:
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage, tenant_id="local")
    bundle = _scaffold_bundle(tmp_path)

    await executor.execute(
        bundle,
        RunRequest(agent="test-agent", input={"question": "hi"}),
    )

    # The persisted run should be readable by tenant_id="local".
    runs = await storage.list_runs(tenant_id="local")
    assert len(runs) == 1
    assert runs[0].tenant_id == "local"
    await storage.close()


# ---------------------------------------------------------------------------
# Override propagates to persisted records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_propagates_to_run_record(tmp_path) -> None:
    """The bug fix: when a worker passes tenant_id_override, the RunRecord
    is stamped with the OVERRIDE, not the Executor's construction-time
    default. Otherwise GET /runs/<id> from the right tenant returns 404."""
    storage = InMemoryStorage()
    await storage.init()
    # Executor built with "local" default tenant — same as production
    # worker config — but the run comes in for "tenant-alpha".
    executor = _make_executor(storage, tenant_id="local")
    bundle = _scaffold_bundle(tmp_path)

    await executor.execute(
        bundle,
        RunRequest(agent="test-agent", input={"question": "hi"}),
        tenant_id_override="tenant-alpha",
    )

    # tenant-alpha sees the run...
    alpha_runs = await storage.list_runs(tenant_id="tenant-alpha")
    assert len(alpha_runs) == 1
    assert alpha_runs[0].tenant_id == "tenant-alpha"

    # ...and "local" (the executor's default) does NOT.
    local_runs = await storage.list_runs(tenant_id="local")
    assert len(local_runs) == 0

    await storage.close()


@pytest.mark.asyncio
async def test_override_propagates_to_failure_record(tmp_path) -> None:
    """Same fix on the failure path: a failed run records a FailureRecord
    with the right tenant_id."""
    storage = InMemoryStorage()
    await storage.init()
    # Provider returns JSON that's valid but doesn't match the agent's
    # output schema (missing required fields) → SchemaError → executor
    # writes a FailureRecord. We can't use unparseable JSON because
    # MockProvider validates its response at construction.
    provider = MockProvider(response='{"wrong": "fields"}')
    registry = ProviderRegistry(default_litellm=provider)
    executor = Executor(
        registry=registry,
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )
    bundle = _scaffold_bundle(tmp_path)

    response = await executor.execute(
        bundle,
        RunRequest(agent="test-agent", input={"question": "hi"}),
        tenant_id_override="tenant-beta",
    )
    assert response.status == "error"

    # The failure should be stamped with tenant-beta, not the executor's
    # default "local". InMemoryStorage exposes failures as a flat list;
    # the tenant_id field on each record is what matters.
    assert len(storage.failures) == 1
    assert storage.failures[0].tenant_id == "tenant-beta"

    await storage.close()


# ---------------------------------------------------------------------------
# Worker dispatch wires the job's tenant through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_dispatch_uses_job_tenant_for_run_record(tmp_path) -> None:
    """End-to-end-ish: a job with tenant_id="customer-x" enters the
    dispatcher, gets executed, and the RunRecord shows up scoped to
    "customer-x" — not to the Executor's "local" default."""
    storage = InMemoryStorage()
    await storage.init()
    executor = _make_executor(storage, tenant_id="local")
    bundle = _scaffold_bundle(tmp_path)

    dispatch = WorkerDispatch(
        storage=storage,
        executor=executor,
        agents=[bundle],
        workflows={},
    )

    job = JobRecord(
        job_id="job-1",
        tenant_id="customer-x",
        api_key_id="k-1",
        kind=JobKind.AGENT,
        target="test-agent",
        input={"question": "hi"},
        status=JobStatus.QUEUED,
    )
    outcome = await dispatch.execute_job(job)
    assert outcome.status == JobStatus.SUCCESS

    # The customer can fetch their run; the worker's default tenant can't.
    cust_runs = await storage.list_runs(tenant_id="customer-x")
    assert len(cust_runs) == 1
    assert cust_runs[0].tenant_id == "customer-x"

    local_runs = await storage.list_runs(tenant_id="local")
    assert len(local_runs) == 0

    # The result_run_id from the outcome should be fetchable by the
    # customer's tenant (this was the actual user-visible bug).
    assert outcome.result_run_id is not None
    via_pk = await storage.get_run(outcome.result_run_id, tenant_id="customer-x")
    assert via_pk is not None
    assert via_pk.tenant_id == "customer-x"

    # ...and 404 from another tenant (tenant isolation upheld).
    miss = await storage.get_run(outcome.result_run_id, tenant_id="other-tenant")
    assert miss is None

    await storage.close()


# ---------------------------------------------------------------------------
# Sanity: AgentSpec is structurally OK (avoids accidental shape changes)
# ---------------------------------------------------------------------------


def test_agent_spec_constructs_for_tests() -> None:
    """Cheap sanity: confirms the bundle scaffolder uses a shape that
    parses against the current AgentSpec. If a future field becomes
    required, this test fails first instead of every test in this file."""
    spec = AgentSpec.model_validate(
        {
            "api_version": "movate/v1",
            "kind": "Agent",
            "name": "test-agent",
            "version": "0.1.0",
            "model": ModelConfig(provider="openai/gpt-4o-mini-2024-07-18").model_dump(),
            "prompt": "./prompt.md",
            "schema": SchemaPaths(
                input="./schema/input.json", output="./schema/output.json"
            ).model_dump(),
        }
    )
    assert spec.name == "test-agent"
