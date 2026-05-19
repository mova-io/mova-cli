"""Tests for ``RemoteExecutor`` — the ``mdk eval <http(s)://>`` seam.

Two layers:

* **Unit**: pin :class:`RemoteExecutor` against an ``httpx.MockTransport``
  that scripts the runtime's three endpoints (POST /run, GET /jobs/{id},
  GET /runs/{id}) for success, error, safety-blocked, timeout, and
  transport failure. Hermetic, no real network.
* **CLI**: exercise the URL detection + required-flag checks in
  ``mdk eval``. Doesn't run an end-to-end eval — that's covered by the
  unit tests of the underlying executor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.client import MovateClient
from movate.core.loader import load_agent
from movate.core.models import (
    JobKind,
    JobStatus,
    Metrics,
    RunRequest,
)
from movate.core.remote_executor import RemoteExecutor

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _build_mock_transport(handlers: dict[str, object]) -> httpx.MockTransport:
    """Wire a dict of {endpoint_label: handler} into an MockTransport.

    ``handlers`` keys are strings the route function checks against; the
    handler value is either a dict (returned as JSON 200) or a callable
    ``(request) -> httpx.Response`` for more control. Endpoints not
    listed return 500 so a missing mock is loud.
    """

    def route(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/run":
            handler = handlers.get("submit")
        elif request.method == "GET" and request.url.path.startswith("/jobs/"):
            handler = handlers.get("get_job")
        elif request.method == "GET" and request.url.path.startswith("/runs/"):
            handler = handlers.get("get_run")
        else:
            return httpx.Response(500, json={"detail": {"error": {"code": "unmocked"}}})

        if handler is None:
            return httpx.Response(500, json={"detail": {"error": {"code": "no_handler"}}})
        if callable(handler):
            return handler(request)
        return httpx.Response(200, json=handler)

    return httpx.MockTransport(route)


def _success_job_payload(job_id: str, run_id: str) -> dict[str, object]:
    return {
        "job_id": job_id,
        "kind": JobKind.AGENT.value,
        "target": "faq-agent",
        "status": JobStatus.SUCCESS.value,
        "input": {"message": "hi"},
        "result_run_id": run_id,
        "error": None,
        "created_at": _now_iso(),
        "claimed_at": _now_iso(),
        "completed_at": _now_iso(),
        "notify_email": None,
    }


def _success_run_payload(run_id: str, job_id: str, output: dict[str, object]) -> dict[str, object]:
    return {
        "run_id": run_id,
        "job_id": job_id,
        "agent": "faq-agent",
        "agent_version": "0.1.0",
        "prompt_hash": "deadbeef",
        "provider": "openai/gpt-4o-mini-2024-07-18",
        "provider_version": "1",
        "pricing_version": "1",
        "status": JobStatus.SUCCESS.value,
        "input": {"message": "hi"},
        "output": output,
        "metrics": Metrics().model_dump(mode="json"),
        "error": None,
        "created_at": _now_iso(),
        "workflow_run_id": None,
        "node_id": None,
    }


@pytest.fixture
def mini_bundle(tmp_path: Path):
    """A minimal agent.yaml on disk so RemoteExecutor has a bundle to
    pass to the runtime. The agent.yaml content isn't transmitted — we
    only need ``bundle.spec.name`` — but load_agent() requires the
    real file structure to construct an AgentBundle."""
    agent_dir = tmp_path / "faq-agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: faq-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n"
    )
    (agent_dir / "prompt.md").write_text("You are helpful.\n\n{{ input.message }}")
    (agent_dir / "schema").mkdir()
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"type": "object", "properties": {"message": {"type": "string"}}})
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps({"type": "object", "properties": {"response": {"type": "string"}}})
    )
    return load_agent(agent_dir)


# ---------------------------------------------------------------------------
# RemoteExecutor unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_remote_executor_happy_path(mini_bundle) -> None:
    """Submit → poll once → fetch → success. Verifies the seam shape:
    RemoteExecutor returns a RunResponse with the agent's output as
    ``data``."""
    job_id = "job-1"
    run_id = "run-1"
    transport = _build_mock_transport(
        {
            "submit": {"job_id": job_id, "status": JobStatus.QUEUED.value},
            "get_job": _success_job_payload(job_id, run_id),
            "get_run": _success_run_payload(run_id, job_id, {"response": "hi back"}),
        }
    )
    client = MovateClient(base_url="http://test", api_key="k", transport=transport)
    executor = RemoteExecutor(client, poll_interval_seconds=0.0)
    try:
        response = await executor.execute(
            mini_bundle, RunRequest(agent="faq-agent", input={"message": "hi"})
        )
    finally:
        await client.aclose()

    assert response.status == "success"
    assert response.data == {"response": "hi back"}
    assert response.run_id == run_id


@pytest.mark.unit
async def test_remote_executor_error_job_propagates(mini_bundle) -> None:
    """ERROR terminal status maps to RunResponse(status=error) with the
    runtime's error envelope verbatim — eval engine treats it as a
    case failure (score 0.0)."""
    transport = _build_mock_transport(
        {
            "submit": {"job_id": "j2", "status": JobStatus.QUEUED.value},
            "get_job": {
                "job_id": "j2",
                "kind": JobKind.AGENT.value,
                "target": "faq-agent",
                "status": JobStatus.ERROR.value,
                "input": {"message": "hi"},
                "result_run_id": None,
                "error": {
                    "type": "provider_error",
                    "message": "upstream 503",
                    "retryable": True,
                },
                "created_at": _now_iso(),
                "claimed_at": _now_iso(),
                "completed_at": _now_iso(),
                "notify_email": None,
            },
        }
    )
    client = MovateClient(base_url="http://test", api_key="k", transport=transport)
    executor = RemoteExecutor(client, poll_interval_seconds=0.0)
    try:
        response = await executor.execute(
            mini_bundle, RunRequest(agent="faq-agent", input={"message": "hi"})
        )
    finally:
        await client.aclose()

    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "provider_error"
    assert "503" in response.error.message


@pytest.mark.unit
async def test_remote_executor_safety_blocked_propagates(mini_bundle) -> None:
    """``safety_blocked`` is its own status, distinct from generic
    error — eval rationale uses it to distinguish model-refused from
    everything-else-broken."""
    transport = _build_mock_transport(
        {
            "submit": {"job_id": "j3", "status": JobStatus.QUEUED.value},
            "get_job": {
                "job_id": "j3",
                "kind": JobKind.AGENT.value,
                "target": "faq-agent",
                "status": JobStatus.SAFETY_BLOCKED.value,
                "input": {"message": "hi"},
                "result_run_id": None,
                "error": {
                    "type": "safety",
                    "message": "blocked by policy",
                    "retryable": False,
                },
                "created_at": _now_iso(),
                "claimed_at": _now_iso(),
                "completed_at": _now_iso(),
                "notify_email": None,
            },
        }
    )
    client = MovateClient(base_url="http://test", api_key="k", transport=transport)
    executor = RemoteExecutor(client, poll_interval_seconds=0.0)
    try:
        response = await executor.execute(
            mini_bundle, RunRequest(agent="faq-agent", input={"message": "hi"})
        )
    finally:
        await client.aclose()

    assert response.status == "safety_blocked"
    assert response.error is not None
    assert response.error.type == "safety"


@pytest.mark.unit
async def test_remote_executor_submit_failure(mini_bundle) -> None:
    """POST /run returning non-2xx surfaces as RunResponse(error) —
    the eval engine doesn't get an exception, the case just fails."""
    transport = _build_mock_transport(
        {
            "submit": lambda req: httpx.Response(
                500, json={"detail": {"error": {"code": "kaboom", "message": "boom"}}}
            ),
        }
    )
    client = MovateClient(base_url="http://test", api_key="k", transport=transport)
    executor = RemoteExecutor(client, poll_interval_seconds=0.0)
    try:
        response = await executor.execute(
            mini_bundle, RunRequest(agent="faq-agent", input={"message": "hi"})
        )
    finally:
        await client.aclose()

    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "submit_failed"


@pytest.mark.unit
async def test_remote_executor_timeout(mini_bundle) -> None:
    """When polling exceeds ``max_wait_seconds`` we return a clean
    timeout-typed RunResponse instead of hanging the whole eval suite."""
    transport = _build_mock_transport(
        {
            "submit": {"job_id": "j4", "status": JobStatus.QUEUED.value},
            # Always RUNNING — never reaches terminal. wait_for_terminal
            # bails out after max_wait_seconds.
            "get_job": {
                "job_id": "j4",
                "kind": JobKind.AGENT.value,
                "target": "faq-agent",
                "status": JobStatus.RUNNING.value,
                "input": {"message": "hi"},
                "result_run_id": None,
                "error": None,
                "created_at": _now_iso(),
                "claimed_at": _now_iso(),
                "completed_at": None,
                "notify_email": None,
            },
        }
    )
    client = MovateClient(base_url="http://test", api_key="k", transport=transport)
    executor = RemoteExecutor(client, poll_interval_seconds=0.0, max_wait_seconds=0.0)
    try:
        response = await executor.execute(
            mini_bundle, RunRequest(agent="faq-agent", input={"message": "hi"})
        )
    finally:
        await client.aclose()

    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "timeout"
    assert response.error.retryable is True


@pytest.mark.unit
async def test_remote_executor_success_without_run_id_is_graceful(mini_bundle) -> None:
    """Defensive: if the runtime returns SUCCESS but no result_run_id,
    we surface a clean error instead of raising — one buggy case
    shouldn't crash the whole eval suite."""
    transport = _build_mock_transport(
        {
            "submit": {"job_id": "j5", "status": JobStatus.QUEUED.value},
            "get_job": {
                "job_id": "j5",
                "kind": JobKind.AGENT.value,
                "target": "faq-agent",
                "status": JobStatus.SUCCESS.value,
                "input": {"message": "hi"},
                # SUCCESS but no result_run_id — runtime bug.
                "result_run_id": None,
                "error": None,
                "created_at": _now_iso(),
                "claimed_at": _now_iso(),
                "completed_at": _now_iso(),
                "notify_email": None,
            },
        }
    )
    client = MovateClient(base_url="http://test", api_key="k", transport=transport)
    executor = RemoteExecutor(client, poll_interval_seconds=0.0)
    try:
        response = await executor.execute(
            mini_bundle, RunRequest(agent="faq-agent", input={"message": "hi"})
        )
    finally:
        await client.aclose()

    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == "missing_run_id"


# ---------------------------------------------------------------------------
# CLI surface tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_url_without_agent_yaml_errors_clean() -> None:
    """When path is a URL, --agent-yaml is required. The CLI surfaces a
    readable error before constructing any HTTP client.

    Uses --mock to skip the live-verify pre-flight (PR #223) so the
    URL-validation error reaches the surface."""
    result = runner.invoke(cli_app, ["eval", "https://example.test/runtime", "--mock"])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "--agent-yaml is required" in combined


@pytest.mark.unit
def test_eval_url_without_api_key_errors_clean(tmp_path: Path) -> None:
    """When path is a URL and there's no API key (no flag, no env var),
    the CLI surfaces a readable error rather than constructing a
    MovateClient that will 401 on every case."""
    # Build a minimal agent.yaml so the --agent-yaml path resolves —
    # we want the API-key check to be the failing rail.
    agent_dir = tmp_path / "faq-agent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: faq-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.json\n"
        "  output: ./schema/output.json\n"
    )
    (agent_dir / "prompt.md").write_text("p")
    (agent_dir / "schema").mkdir()
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"type": "object", "properties": {"message": {"type": "string"}}})
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps({"type": "object", "properties": {"response": {"type": "string"}}})
    )

    result = runner.invoke(
        cli_app,
        [
            "eval",
            "https://example.test/runtime",
            "--agent-yaml",
            str(agent_dir),
            "--mock",  # skip the live-verify pre-flight (PR #223)
        ],
        env={"MOVATE_API_KEY": ""},
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "no API key" in combined or "MOVATE_API_KEY" in combined
