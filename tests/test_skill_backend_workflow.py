"""WorkflowDispatchSkillBackend — kind: workflow (ADR 077 D1/D2).

Covers the new handoff logic: mock short-circuit, await-mode outcome,
detach-mode handle, the HITL paused outcome, and the error paths
(non-success job, missing connection config). MovateClient is stubbed so these
run with no live runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import movate.core.client as client_mod
from movate.core.models import JobKind, SkillImplementationKind
from movate.core.skill_backend import SkillError, SkillErrorType, SkillExecutionContext
from movate.core.skill_backend.workflow import WorkflowDispatchSkillBackend

pytestmark = pytest.mark.unit


def _bundle(*, target: str = "pos-reboot", timeout_s: int = 30) -> Any:
    return SimpleNamespace(
        spec=SimpleNamespace(
            name="dispatch-workflow",
            implementation=SimpleNamespace(target_workflow=target, timeout_s=timeout_s),
        )
    )


class _FakeClient:
    """Async-context-manager stand-in for MovateClient."""

    def __init__(
        self,
        *,
        job_status: str = "success",
        run_id: str = "wf-run-1",
        wf_status: str = "completed",
        final_state: dict | None = None,
        human_task: dict | None = None,
        job_error: Any = None,
        calls: list | None = None,
        **_kw: Any,
    ) -> None:
        self._job_status = job_status
        self._run_id = run_id
        self._wf_status = wf_status
        self._final_state = final_state
        self._human_task = human_task
        self._job_error = job_error
        self.calls = calls if calls is not None else []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def submit_job(self, *, kind: JobKind, target: str, input: dict) -> Any:
        self.calls.append(("submit", kind, target, input))
        return SimpleNamespace(job_id=self._run_id)

    async def wait_for_terminal(self, job_id: str, **_kw: Any) -> Any:
        self.calls.append(("wait", job_id))
        return SimpleNamespace(
            status=SimpleNamespace(value=self._job_status),
            error=self._job_error,
            result_run_id=self._run_id,
        )

    async def list_workflow_runs(self, *, limit: int = 20, status: Any = None) -> Any:
        self.calls.append(("list", limit))
        run = SimpleNamespace(
            workflow_run_id=self._run_id,
            status=SimpleNamespace(value=self._wf_status),
            final_state=self._final_state,
            human_task=self._human_task,
        )
        return SimpleNamespace(workflow_runs=[run], count=1)


def _patch_client(monkeypatch, **kwargs) -> list:
    """Install a _FakeClient factory + dummy connection env; return the call log."""
    calls: list = []

    def factory(**_client_kwargs: Any) -> _FakeClient:
        return _FakeClient(calls=calls, **kwargs)

    monkeypatch.setattr(client_mod, "MovateClient", factory)
    monkeypatch.setenv("MOVATE_RUNTIME_URL", "http://runtime")
    monkeypatch.setenv("MOVATE_API_KEY", "k")
    return calls


def test_kind_is_workflow():
    assert WorkflowDispatchSkillBackend().kind == SkillImplementationKind.WORKFLOW


async def test_mock_short_circuits(monkeypatch):
    # mock must not touch MovateClient at all
    monkeypatch.delenv("MOVATE_RUNTIME_URL", raising=False)
    out = await WorkflowDispatchSkillBackend().execute(
        _bundle(), {"store": "118", "lane": "5"}, SkillExecutionContext(mock=True)
    )
    assert out["_workflow_skill_mock"] is True
    assert out["store"] == "118"


async def test_await_returns_outcome(monkeypatch):
    calls = _patch_client(monkeypatch, wf_status="completed", final_state={"reply": "fixed"})
    out = await WorkflowDispatchSkillBackend().execute(
        _bundle(), {"store": "118", "lane": "5"}, SkillExecutionContext()
    )
    assert out["run_id"] == "wf-run-1"
    assert out["status"] == "completed"
    assert out["state"] == {"reply": "fixed"}
    kinds = [c for c in calls if c[0] == "submit"]
    assert kinds and kinds[0][1] == JobKind.WORKFLOW and kinds[0][2] == "pos-reboot"
    # the control key is stripped from the dispatched initial state
    assert "mode" not in kinds[0][3]
    assert any(c[0] == "wait" for c in calls)  # await mode blocked on terminal


async def test_detach_returns_handle_without_waiting(monkeypatch):
    calls = _patch_client(monkeypatch)
    out = await WorkflowDispatchSkillBackend().execute(
        _bundle(), {"store": "118", "lane": "5", "mode": "detach"}, SkillExecutionContext()
    )
    assert out["status"] == "dispatched"
    assert out["run_id"] == "wf-run-1"
    assert not any(c[0] == "wait" for c in calls)  # detach never blocks


async def test_paused_outcome_is_escalation(monkeypatch):
    _patch_client(
        monkeypatch,
        wf_status="paused",
        human_task={"prompt": "approve dispatch?"},
        final_state=None,
    )
    out = await WorkflowDispatchSkillBackend().execute(
        _bundle(), {"store": "204", "lane": "9"}, SkillExecutionContext()
    )
    assert out["status"] == "paused"
    assert out["human_task"] == {"prompt": "approve dispatch?"}
    assert "human" in out["summary"].lower()


async def test_nonsuccess_job_raises(monkeypatch):
    _patch_client(monkeypatch, job_status="error", job_error=SimpleNamespace(message="boom"))
    with pytest.raises(SkillError) as ei:
        await WorkflowDispatchSkillBackend().execute(
            _bundle(), {"store": "1", "lane": "2"}, SkillExecutionContext()
        )
    assert ei.value.type == SkillErrorType.BACKEND_ERROR


async def test_missing_config_raises(monkeypatch):
    monkeypatch.delenv("MOVATE_RUNTIME_URL", raising=False)
    monkeypatch.delenv("MOVATE_API_KEY", raising=False)
    with pytest.raises(SkillError) as ei:
        await WorkflowDispatchSkillBackend().execute(
            _bundle(), {"store": "1", "lane": "2"}, SkillExecutionContext()
        )
    assert ei.value.type == SkillErrorType.BACKEND_ERROR
