"""Tests for ``mdk explain``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import ErrorInfo, JobStatus, Metrics, RunRecord, TokenUsage

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    run_id: str = "run-abc",
    agent: str = "faq-agent",
    agent_version: str = "0.1.0",
    status: str = JobStatus.SUCCESS,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    error: ErrorInfo | None = None,
    latency_ms: int = 42,
    cost_usd: float = 0.000019,
    tokens_in: int = 312,
    tokens_out: int = 87,
    tokens_cached: int = 0,
    provider: str = "openai/gpt-4o-mini-2024-07-18",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id="job-1",
        tenant_id="local",
        agent=agent,
        agent_version=agent_version,
        prompt_hash="abc123",
        provider=provider,
        provider_version="1.0",
        pricing_version="2026",
        status=status,
        input=input or {"question": "What is the return policy?"},
        output=output,
        metrics=Metrics(
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            tokens=TokenUsage(input=tokens_in, output=tokens_out, cached_input=tokens_cached),
            provider=provider,
        ),
        error=error,
        created_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
    )


class _FakeStorage:
    """In-memory storage stub for explain tests (mirrors test_logs_cmd pattern)."""

    def __init__(self, records: list[RunRecord]) -> None:
        self._records = {r.run_id: r for r in records}
        self._list = records

    async def init(self) -> None:
        pass

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        return self._records.get(run_id)

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]:
        records = self._list
        if agent:
            records = [r for r in records if r.agent == agent]
        return records[:limit]


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_known_run_shows_decision_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <run-id> prints the run id, agent, input, LLM call, and output."""
    rec = _make_run(
        output={"answer": "Our return policy is 30 days...", "confidence": 0.9},
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-abc" in result.stdout
    assert "faq-agent" in result.stdout
    assert "0.1.0" in result.stdout
    # Input section
    assert "What is the return policy" in result.stdout
    # LLM call section
    assert "LLM call" in result.stdout
    assert "gpt-4o-mini" in result.stdout
    assert "312" in result.stdout  # tokens_in
    assert "87" in result.stdout  # tokens_out
    assert "42" in result.stdout  # latency_ms
    # Output section
    assert "return policy is 30 days" in result.stdout


@pytest.mark.unit
def test_explain_unknown_run_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <unknown-id> exits 1 with 'run not found'."""
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([]))

    result = runner.invoke(app, ["explain", "no-such-run"])

    assert result.exit_code == 1
    assert "not found" in result.stderr


@pytest.mark.unit
def test_explain_last_shows_most_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain --last shows the most-recent run without requiring a run ID."""
    rec = _make_run(
        run_id="run-xyz",
        agent="kb-agent",
        output={"answer": "yes"},
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "--last"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-xyz" in result.stdout
    assert "kb-agent" in result.stdout


@pytest.mark.unit
def test_explain_json_flag_emits_machine_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <id> --json emits valid JSON with the key fields."""
    rec = _make_run(output={"answer": "30 days"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["run_id"] == "run-abc"
    assert parsed["agent"] == "faq-agent"
    assert parsed["status"] == JobStatus.SUCCESS
    assert parsed["input"] == {"question": "What is the return policy?"}
    assert parsed["output"] == {"answer": "30 days"}
    assert "llm_call" in parsed
    assert parsed["llm_call"]["tokens_in"] == 312
    assert parsed["llm_call"]["tokens_out"] == 87
    assert parsed["llm_call"]["latency_ms"] == 42


@pytest.mark.unit
def test_explain_error_run_shows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain on a failed run shows the error type and message."""
    rec = _make_run(
        status=JobStatus.ERROR,
        output=None,
        error=ErrorInfo(type="provider_error", message="rate limit exceeded"),
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "provider_error" in result.stdout
    assert "rate limit exceeded" in result.stdout


@pytest.mark.unit
def test_explain_cost_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain shows the cost when it is non-zero."""
    rec = _make_run(cost_usd=0.000019, output={"answer": "yes"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "0.000019" in result.stdout


@pytest.mark.unit
def test_explain_cached_tokens_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain shows cached token count when non-zero."""
    rec = _make_run(tokens_cached=200, output={"answer": "cached"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "cached: 200" in result.stdout


@pytest.mark.unit
def test_explain_tracer_hint_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain always shows the MOVATE_TRACER hint for step-level tracing."""
    rec = _make_run(output={"answer": "yes"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "MOVATE_TRACER" in result.stdout


@pytest.mark.unit
def test_explain_success_status_icon(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain marks a successful run with a success indicator."""
    rec = _make_run(status=JobStatus.SUCCESS, output={"ok": True})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0
    assert "success" in result.stdout


@pytest.mark.unit
def test_explain_error_json_includes_error_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain --json for an error run includes the error dict."""
    rec = _make_run(
        status=JobStatus.ERROR,
        output=None,
        error=ErrorInfo(type="timeout", message="call timed out", retryable=True),
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["error"]["type"] == "timeout"
    assert parsed["error"]["message"] == "call timed out"
    assert parsed["output"] is None
