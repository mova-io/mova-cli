"""Run-usage metrics on the *synchronous* runtime edges.

``mdk.run.tokens`` / ``mdk.run.cost_usd`` measure per-run token + cost volume.
They were originally recorded only in the async job-worker dispatch
(:func:`movate.runtime.dispatch.WorkerDispatch._execute_agent`). But the
synchronous run transports — inline ``?wait=true``, the streaming SSE run, and
the OpenAI-compatible ``/v1/chat/completions`` shim — bypass the worker, so
playground + OpenWebUI traffic emitted ``agent.execute`` spans yet never the
run-usage metrics. Cost/token dashboards stayed empty.

These tests pin the fix: :func:`movate.runtime.app._record_run_usage_edge`
records usage from the ``RunResponse`` in hand, and the inline endpoint calls
it on a cost-incurring run. The three call sites share that one helper, so the
helper's own unit coverage + one live-edge integration is proportionate.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, ApiKeyEnv, mint_api_key
from movate.runtime import build_app
from movate.runtime.app import _record_run_usage_edge
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# Unit — the shared helper
# ---------------------------------------------------------------------------


def _fake_response(tokens_in: int, tokens_out: int, cost: float) -> SimpleNamespace:
    return SimpleNamespace(
        metrics=SimpleNamespace(
            tokens=SimpleNamespace(input=tokens_in, output=tokens_out),
            cost_usd=cost,
        )
    )


@pytest.mark.unit
def test_helper_records_summed_tokens_and_cost() -> None:
    """The helper sums input + output tokens and forwards cost verbatim."""
    with patch("movate.runtime.app.record_run_usage") as rec:
        _record_run_usage_edge("tenant-a", _fake_response(7, 11, 0.0042))
    rec.assert_called_once()
    kwargs = rec.call_args.kwargs
    assert kwargs["tenant_id"] == "tenant-a"
    assert kwargs["tokens"] == 18
    assert kwargs["cost_usd"] == 0.0042


@pytest.mark.unit
def test_helper_noop_when_metrics_absent() -> None:
    """A RunResponse without ``.metrics`` records nothing (never a fake zero)."""
    with patch("movate.runtime.app.record_run_usage") as rec:
        _record_run_usage_edge("tenant-a", SimpleNamespace(metrics=None))
        _record_run_usage_edge("tenant-a", SimpleNamespace())
    rec.assert_not_called()


@pytest.mark.unit
def test_helper_is_fail_soft() -> None:
    """A metrics hiccup must never propagate out of a run response."""
    with patch("movate.runtime.app.record_run_usage", side_effect=RuntimeError("boom")):
        # Must not raise.
        _record_run_usage_edge("tenant-a", _fake_response(1, 1, 0.0))


# ---------------------------------------------------------------------------
# Integration — the inline ?wait=true edge actually calls the helper
# ---------------------------------------------------------------------------

_AGENT_YAML = b"""\
api_version: movate/v1
kind: Agent
name: usage-demo
version: 0.1.0
description: demo for run-usage metric tests
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
"""
_PROMPT = b"Hi {{ input.text }}\n"
_INPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
).encode()
# Matches the MockProvider's default ``{"message": "mock response"}`` so the run
# SUCCEEDS (and carries real token usage) rather than failing output validation.
_OUTPUT_SCHEMA = json.dumps(
    {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}
).encode()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage, tmp_path: Path) -> TestClient:
    agents = tmp_path / "agents"
    agents.mkdir()
    return TestClient(build_app(storage, agents_path=agents))


@pytest.fixture
async def auth_header(storage: InMemoryStorage) -> dict[str, str]:
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="usage-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}


def _create_agent(client: TestClient, auth_header: dict[str, str]) -> None:
    r = client.post(
        "/api/v1/agents",
        files=[
            ("agent_yaml", ("agent.yaml", _AGENT_YAML, "application/x-yaml")),
            ("prompt", ("prompt.md", _PROMPT, "text/markdown")),
            ("input_schema", ("input.json", _INPUT_SCHEMA, "application/json")),
            ("output_schema", ("output.json", _OUTPUT_SCHEMA, "application/json")),
        ],
        headers=auth_header,
    )
    assert r.status_code == 201, r.text


@pytest.mark.unit
def test_inline_run_records_run_usage(client: TestClient, auth_header: dict[str, str]) -> None:
    """An inline ``?wait=true`` run (a synchronous edge that bypasses the job
    worker) records ``mdk.run.tokens`` / ``mdk.run.cost_usd`` — so the
    playground's cost/token dashboards see this traffic, not just spans."""
    _create_agent(client, auth_header)
    with patch("movate.runtime.app.record_run_usage") as rec:
        r = client.post(
            "/api/v1/agents/usage-demo/runs?wait=true",
            json={"input": {"text": "hello"}, "mock": True},
            headers=auth_header,
        )
    assert r.status_code == 200, r.text
    rec.assert_called_once()
    kwargs = rec.call_args.kwargs
    # MockProvider yields deterministic non-zero token usage.
    assert kwargs["tokens"] >= 1
    assert kwargs["cost_usd"] >= 0.0
    assert kwargs["tenant_id"]  # tenant scoped, non-empty
