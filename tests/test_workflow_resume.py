"""Resume-workflow surface tests — lookup, tenant isolation, error paths.

The full LangGraph ``update_state`` + ``ainvoke(None)`` integration
lands with the HITL PR (Tier 2 #4) where there's a real paused
workflow to exercise against. This PR ships the SURFACE — the function
signature, the storage lookup, tenant-isolation check, and the
checkpointer-required error path — so the HITL PR has a clean place to
plug the LangGraph body into.

Coverage:

* Resume on a missing run_id raises ``ResumeNotFound`` (HTTP-404 shape).
* Resume on a cross-tenant run_id raises the same ``ResumeNotFound``
  — leaking ``ResumeError`` here would tell tenant B that tenant A's
  run exists. We don't.
* Resume on a workflow without a checkpointer raises ``ResumeError``
  with an actionable pointer to the YAML field.
* The function accepts ``payload=None`` and ``payload=<dict>``
  identically up to the LangGraph call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("langgraph")

from movate.core.executor import Executor
from movate.core.models import WorkflowRunRecord, WorkflowStatus
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.core.workflow.resume import (
    ResumeError,
    ResumeNotFound,
    resume_workflow,
)
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"text": {"type": "string"}, "approved": {"type": "boolean"}},
}


def _make_agent(agent_dir: Path) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompt.md").write_text("echo {{ input.text }}\n")
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"type": "object", "properties": {"text": {"type": "string"}}})
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["text"],
                "additionalProperties": False,
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": "ag",
                "version": "0.1.0",
                "lifecycle": "validated",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
            }
        )
    )


def _scaffold_workflow(tmp_path: Path, *, checkpointer: str | None) -> Path:
    workflow_dir = tmp_path / f"wf-resume-{checkpointer or 'none'}"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    _make_agent(workflow_dir / "agents" / "n1")
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    yaml_path = workflow_dir / "workflow.yaml"
    payload: dict = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "resume-test",
        "version": "0.1.0",
        "runtime": "langgraph",
        "state_schema": "./state.json",
        "entrypoint": "n1",
        "nodes": [{"id": "n1", "type": "agent", "ref": "./agents/n1"}],
        "edges": [],
    }
    if checkpointer:
        payload["checkpointer"] = checkpointer
    yaml_path.write_text(yaml.safe_dump(payload))
    return yaml_path


class _StubProvider(BaseLLMProvider):
    name = "stub"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(text='{"text": "ok"}')

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def executor(pricing: PricingTable, storage: InMemoryStorage) -> Executor:
    return Executor(
        provider=_StubProvider(),
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )


# ---------------------------------------------------------------------------
# Lookup — missing + cross-tenant return the same ResumeNotFound
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resume_missing_run_id_raises_not_found(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    yaml_path = _scaffold_workflow(tmp_path, checkpointer="memory")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    with pytest.raises(ResumeNotFound, match="no workflow run found"):
        await resume_workflow(
            "wf-does-not-exist",
            payload=None,
            graph=graph,
            executor=executor,
            storage=storage,
            tenant_id="acme",
        )


@pytest.mark.unit
async def test_resume_cross_tenant_returns_not_found(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    """Tenant A's run is not visible to tenant B — same ResumeNotFound
    response as a missing id. We deliberately don't surface a different
    error type because that would leak the existence of cross-tenant
    runs."""
    yaml_path = _scaffold_workflow(tmp_path, checkpointer="memory")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    # Seed a workflow run under tenant A.
    await storage.save_workflow_run(
        WorkflowRunRecord(
            workflow_run_id="wf-tenant-a-123",
            tenant_id="acme",
            workflow="resume-test",
            workflow_version="0.1.0",
            status=WorkflowStatus.SUCCESS,
            initial_state={"text": "seed"},
            final_state={"text": "ok"},
        )
    )

    # Tenant B tries to resume it — should look exactly like a 404.
    with pytest.raises(ResumeNotFound):
        await resume_workflow(
            "wf-tenant-a-123",
            payload=None,
            graph=graph,
            executor=executor,
            storage=storage,
            tenant_id="globex",
        )


# ---------------------------------------------------------------------------
# Required-checkpointer check
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resume_without_checkpointer_raises_resume_error(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    """Workflows without `checkpointer:` set can't be resumed — there's
    nothing on disk to continue from. ResumeError points operators at
    the YAML field."""
    yaml_path = _scaffold_workflow(tmp_path, checkpointer=None)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    await storage.save_workflow_run(
        WorkflowRunRecord(
            workflow_run_id="wf-no-cp",
            tenant_id="acme",
            workflow="resume-test",
            workflow_version="0.1.0",
            status=WorkflowStatus.SUCCESS,
            initial_state={"text": "seed"},
            final_state={"text": "ok"},
        )
    )

    with pytest.raises(ResumeError, match="checkpointer"):
        await resume_workflow(
            "wf-no-cp",
            payload=None,
            graph=graph,
            executor=executor,
            storage=storage,
            tenant_id="acme",
        )


# ---------------------------------------------------------------------------
# Surface — payload is accepted; final_state reflects merge
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resume_returns_merged_state_when_record_exists(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    """For v1 (pre-HITL), the resume function returns a WorkflowResult
    whose ``final_state`` is the record's final_state shallow-merged
    with the supplied payload — operators can preview what the next
    invocation would see. The LangGraph ``ainvoke(None)`` call lands
    with the HITL PR."""
    yaml_path = _scaffold_workflow(tmp_path, checkpointer="memory")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    await storage.save_workflow_run(
        WorkflowRunRecord(
            workflow_run_id="wf-merge-test",
            tenant_id="acme",
            workflow="resume-test",
            workflow_version="0.1.0",
            status=WorkflowStatus.SUCCESS,
            initial_state={"text": "seed"},
            final_state={"text": "checkpoint", "step": 1},
        )
    )

    result = await resume_workflow(
        "wf-merge-test",
        payload={"approved": True, "step": 2},
        graph=graph,
        executor=executor,
        storage=storage,
        tenant_id="acme",
    )

    assert result.status is WorkflowStatus.SUCCESS
    assert result.workflow_run_id == "wf-merge-test"
    # Payload merges over the checkpointed state — step=2 wins.
    assert result.final_state == {"text": "checkpoint", "step": 2, "approved": True}


@pytest.mark.unit
async def test_resume_with_none_payload_preserves_state_as_is(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    yaml_path = _scaffold_workflow(tmp_path, checkpointer="memory")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    await storage.save_workflow_run(
        WorkflowRunRecord(
            workflow_run_id="wf-no-payload",
            tenant_id="acme",
            workflow="resume-test",
            workflow_version="0.1.0",
            status=WorkflowStatus.SUCCESS,
            initial_state={"text": "seed"},
            final_state={"text": "ok"},
        )
    )

    result = await resume_workflow(
        "wf-no-payload",
        payload=None,
        graph=graph,
        executor=executor,
        storage=storage,
        tenant_id="acme",
    )
    assert result.final_state == {"text": "ok"}
