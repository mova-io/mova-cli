"""Tests for ``mdk logs``."""

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
    agent: str = "faq-bot",
    status: str = JobStatus.SUCCESS,
    output: dict[str, Any] | None = None,
    error: ErrorInfo | None = None,
    workflow_run_id: str | None = None,
    node_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id="job-1",
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="abc123",
        provider="anthropic/claude-haiku-4-5",
        provider_version="1.0",
        pricing_version="2026",
        status=status,
        input={"question": "What is movate?"},
        output=output or {"answer": "A platform for agents.", "confidence": 0.9},
        metrics=Metrics(
            latency_ms=420,
            cost_usd=0.00012,
            tokens=TokenUsage(input=150, output=80, cached_input=20),
        ),
        error=error,
        created_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )


class _FakeStorage:
    """In-memory storage stub for logs tests."""

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
# Unit tests (monkeypatched storage)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_logs_known_run_shows_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs <run-id> prints agent name, latency, cost, input, output."""
    rec = _make_run()
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([rec]),
    )
    result = runner.invoke(app, ["logs", "run-abc"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-abc" in result.stdout
    assert "faq-bot" in result.stdout
    assert "420" in result.stdout  # latency_ms
    assert "0.00012" in result.stdout  # cost_usd
    assert "What is movate" in result.stdout  # input
    assert "platform for agents" in result.stdout  # output


@pytest.mark.unit
def test_logs_unknown_run_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs <unknown-id> exits 1 with a 'run not found' message."""
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([]),
    )
    result = runner.invoke(app, ["logs", "no-such-run"])
    assert result.exit_code == 1
    assert "not found" in result.stderr


@pytest.mark.unit
def test_logs_last_shows_most_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs --last shows the most-recent run without requiring a run ID."""
    rec = _make_run(run_id="run-xyz", agent="my-bot")
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([rec]),
    )
    result = runner.invoke(app, ["logs", "--last"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-xyz" in result.stdout
    assert "my-bot" in result.stdout


@pytest.mark.unit
def test_logs_last_agent_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs --last --agent <name> scopes to that agent."""
    a = _make_run(run_id="run-a", agent="agent-a")
    b = _make_run(run_id="run-b", agent="agent-b")
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([a, b]),
    )
    result = runner.invoke(app, ["logs", "--last", "--agent", "agent-b"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-b" in result.stdout
    assert "run-a" not in result.stdout


@pytest.mark.unit
def test_logs_raw_prints_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs <id> --raw prints the raw RunRecord JSON."""
    rec = _make_run()
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([rec]),
    )
    result = runner.invoke(app, ["logs", "run-abc", "--raw"])
    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["run_id"] == "run-abc"
    assert parsed["agent"] == "faq-bot"


@pytest.mark.unit
def test_logs_error_run_shows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs on a failed run shows the error message."""
    rec = _make_run(
        status=JobStatus.ERROR,
        output=None,
        error=ErrorInfo(type="provider_error", message="rate limit exceeded"),
    )
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([rec]),
    )
    result = runner.invoke(app, ["logs", "run-abc"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "rate limit exceeded" in result.stdout
    assert "provider_error" in result.stdout


@pytest.mark.unit
def test_logs_workflow_run_shows_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs on a workflow-linked run shows workflow_run_id and node_id."""
    rec = _make_run(
        workflow_run_id="wf-run-99",
        node_id="triage",
    )
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([rec]),
    )
    result = runner.invoke(app, ["logs", "run-abc"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "wf-run-99" in result.stdout
    assert "triage" in result.stdout


@pytest.mark.unit
def test_logs_token_usage_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs shows token usage including cached tokens."""
    rec = _make_run()
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([rec]),
    )
    result = runner.invoke(app, ["logs", "run-abc"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # 150→80 tok with 20 cached
    assert "150" in result.stdout
    assert "80" in result.stdout


@pytest.mark.unit
def test_logs_no_args_shows_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk logs with no arguments shows the most-recent run (same as --last)."""
    rec = _make_run(run_id="run-latest")
    monkeypatch.setattr(
        "movate.cli.logs.build_storage",
        lambda: _FakeStorage([rec]),
    )
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-latest" in result.stdout
