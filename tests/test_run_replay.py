"""Agent run replay — core diff math + CLI integration."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import (
    ErrorInfo,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.core.run_replay import (
    AgentReplayDiff,
    ReplayMismatchError,
    render_replay_json,
    replay_agent_run,
)
from movate.providers.mock import MockProvider
from movate.providers.pricing import load_pricing
from movate.testing import InMemoryStorage, NullTracer

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_record(
    *,
    run_id: str | None = None,
    agent: str = "demo-agent",
    input_payload: dict | None = None,
    output: dict | None = None,
    status: JobStatus = JobStatus.SUCCESS,
    cost: float = 0.0001,
    latency_ms: int = 100,
) -> RunRecord:
    return RunRecord(
        run_id=run_id or str(uuid4()),
        job_id=str(uuid4()),
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="abc123def456789",
        provider="mock/v0",
        provider_version="0.0.1",
        pricing_version="2026.05.01",
        status=status,
        input=input_payload or {"text": "hello"},
        output=output if status is JobStatus.SUCCESS else None,
        metrics=Metrics(
            latency_ms=latency_ms,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=cost,
            provider="mock/v0",
            pricing_version="2026.05.01",
        ),
        error=ErrorInfo(type="schema_error", message="bad", retryable=False)
        if status is JobStatus.ERROR
        else None,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Diff math (no I/O)
# ---------------------------------------------------------------------------


def _make_response(*, status: str = "success", data: dict | None = None, cost: float = 0.0002):
    """RunResponse builder; lazy import keeps the module-level test imports tidy."""
    from movate.core.models import RunResponse  # noqa: PLC0415

    return RunResponse(
        status=status,  # type: ignore[arg-type]
        data=data or {},
        metrics=Metrics(
            latency_ms=120,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=cost,
            provider="mock/v0",
            pricing_version="2026.05.01",
        ),
    )


@pytest.mark.unit
def test_diff_output_unchanged_when_data_matches() -> None:
    rec = _make_record(output={"reply": "hi"})
    resp = _make_response(data={"reply": "hi"})
    diff = AgentReplayDiff(original=rec, current=resp)
    assert diff.output_changed is False
    assert diff.changed_keys == []


@pytest.mark.unit
def test_diff_output_changed_lists_keys() -> None:
    rec = _make_record(output={"reply": "hi", "score": 0.5})
    resp = _make_response(data={"reply": "ho", "score": 0.5, "extra": True})
    diff = AgentReplayDiff(original=rec, current=resp)
    assert diff.output_changed is True
    # `reply` differs; `extra` is new — both flagged. `score` matches.
    assert diff.changed_keys == ["extra", "reply"]


@pytest.mark.unit
def test_diff_status_flip_is_status_changed() -> None:
    rec = _make_record(status=JobStatus.SUCCESS, output={"reply": "ok"})
    resp = _make_response(status="error", data={})
    diff = AgentReplayDiff(original=rec, current=resp)
    assert diff.status_changed is True
    # On status-error replay we don't enumerate top-level keys.
    assert diff.changed_keys == []


@pytest.mark.unit
def test_diff_cost_and_latency_deltas() -> None:
    rec = _make_record(cost=0.0001, latency_ms=100)
    resp = _make_response(cost=0.0003)  # latency hard-coded to 120 in builder
    diff = AgentReplayDiff(original=rec, current=resp)
    assert diff.cost_delta_usd == pytest.approx(0.0002)
    assert diff.latency_delta_ms == 20


# ---------------------------------------------------------------------------
# replay_agent_run — async with InMemoryStorage + Executor + MockProvider
# ---------------------------------------------------------------------------


@pytest.fixture
def faq_bundle(tmp_path: Path):
    """Scaffold a default agent template into tmp_path and return its bundle.

    Reuses the `init` template indirectly so every test gets a real bundle
    (loader, schemas, prompt) without coupling to that command's CLI.
    """
    from movate.cli.main import app  # noqa: PLC0415

    result = runner.invoke(
        app, ["init", "--bare", "demo-agent", "-t", "default", "--target", str(tmp_path)]
    )
    assert result.exit_code == 0
    return load_agent(tmp_path / "demo-agent")


@pytest.mark.unit
async def test_replay_raises_when_run_id_missing(faq_bundle, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    storage = InMemoryStorage()
    await storage.init()
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )

    with pytest.raises(ReplayMismatchError, match="no run found"):
        await replay_agent_run(
            storage=storage,
            executor=executor,
            bundle=faq_bundle,
            run_id="ghost-id",
        )


@pytest.mark.unit
async def test_replay_raises_on_agent_name_mismatch(faq_bundle, monkeypatch) -> None:
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    storage = InMemoryStorage()
    await storage.init()
    # Record stored against a *different* agent name than the bundle.
    rec = _make_record(agent="other-agent", input_payload={"text": "hi"})
    await storage.save_run(rec)

    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )

    with pytest.raises(ReplayMismatchError, match="agent mismatch"):
        await replay_agent_run(
            storage=storage,
            executor=executor,
            bundle=faq_bundle,
            run_id=rec.run_id,
        )


@pytest.mark.unit
async def test_replay_executes_and_returns_diff(faq_bundle, monkeypatch) -> None:
    """Round-trip: save a record, replay it, get a populated diff back."""
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    storage = InMemoryStorage()
    await storage.init()
    rec = _make_record(
        agent=faq_bundle.spec.name,
        input_payload={"text": "hi"},
        output={"message": "Hello!"},
    )
    await storage.save_run(rec)

    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )

    diff = await replay_agent_run(
        storage=storage,
        executor=executor,
        bundle=faq_bundle,
        run_id=rec.run_id,
    )
    assert diff.original.run_id == rec.run_id
    assert diff.current.status == "success"
    # Mock returns the same shape → no diff in keys.
    assert diff.output_changed is False


@pytest.mark.unit
async def test_replay_surfaces_output_change_when_mock_diverges(faq_bundle, monkeypatch) -> None:
    """The whole point: the agent now returns something different than the record."""
    storage = InMemoryStorage()
    await storage.init()
    rec = _make_record(
        agent=faq_bundle.spec.name,
        input_payload={"text": "hi"},
        output={"message": "OLD answer"},
    )
    await storage.save_run(rec)

    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "NEW answer"}')
    executor = Executor(
        provider=MockProvider(),
        pricing=load_pricing(),
        storage=storage,
        tracer=NullTracer(),
        tenant_id="local",
    )

    diff = await replay_agent_run(
        storage=storage,
        executor=executor,
        bundle=faq_bundle,
        run_id=rec.run_id,
    )
    assert diff.output_changed is True
    assert diff.changed_keys == ["message"]


# ---------------------------------------------------------------------------
# render_replay_json — shape contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_replay_json_shape() -> None:
    rec = _make_record(output={"reply": "old"})
    resp = _make_response(data={"reply": "new"})
    diff = AgentReplayDiff(original=rec, current=resp)
    payload = render_replay_json(diff)

    assert payload["run_id"] == rec.run_id
    assert payload["agent"] == rec.agent
    assert payload["recorded"]["status"] == "success"
    assert payload["current"]["status"] == "success"
    assert payload["diff"]["output_changed"] is True
    assert payload["diff"]["changed_keys"] == ["reply"]
    assert payload["diff"]["cost_delta_usd"] == pytest.approx(0.0001)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _scaffold_default_agent(parent: Path) -> Path:
    result = runner.invoke(
        app, ["init", "--bare", "demo-agent", "-t", "default", "--target", str(parent)]
    )
    assert result.exit_code == 0, result.stdout
    return parent / "demo-agent"


def _read_latest_run_id(home: Path) -> str:
    db_path = home / ".movate" / "local.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None, "expected a runs row"
    return row[0]


@pytest.mark.unit
def test_cli_replay_matches_recorded_output_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    # Step 1: real run to populate storage.
    initial = runner.invoke(
        app,
        ["run", str(agent_dir), '{"text": "hi"}', "--mock"],
    )
    assert initial.exit_code == 0, initial.stdout
    run_id = _read_latest_run_id(tmp_path)

    # Step 2: replay with same mock → identical output.
    replay = runner.invoke(
        app,
        ["run", str(agent_dir), "--replay", run_id, "--mock"],
    )
    assert replay.exit_code == 0, replay.stdout
    payload = json.loads(replay.stdout)
    assert payload["run_id"] == run_id
    assert payload["diff"]["output_changed"] is False


@pytest.mark.unit
def test_cli_replay_surfaces_diverged_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "ORIGINAL"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    initial = runner.invoke(
        app,
        ["run", str(agent_dir), '{"text": "hi"}', "--mock"],
    )
    assert initial.exit_code == 0, initial.stdout
    run_id = _read_latest_run_id(tmp_path)

    # Now divergence: different mock response simulates a code/prompt change.
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "CHANGED"}')
    replay = runner.invoke(
        app,
        ["run", str(agent_dir), "--replay", run_id, "--mock"],
    )
    # Output diff is not a failure — engineer is debugging, surface and exit 0.
    assert replay.exit_code == 0, replay.stdout
    payload = json.loads(replay.stdout)
    assert payload["diff"]["output_changed"] is True
    assert payload["diff"]["changed_keys"] == ["message"]
    assert payload["recorded"]["output"]["message"] == "ORIGINAL"
    assert payload["current"]["output"]["message"] == "CHANGED"


@pytest.mark.unit
def test_cli_replay_unknown_run_id_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    result = runner.invoke(
        app,
        ["run", str(agent_dir), "--replay", "no-such-id", "--mock"],
    )
    assert result.exit_code == 2, result.stdout
    assert "no run found" in result.stderr


@pytest.mark.unit
def test_cli_replay_rejects_combined_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--replay is mutually exclusive with positional INPUT."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    result = runner.invoke(
        app,
        ["run", str(agent_dir), '{"text":"hi"}', "--replay", "abc", "--mock"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


@pytest.mark.unit
def test_cli_replay_text_format_renders_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-o text` prints the Rich table to stderr and a slim JSON to stdout."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "Hello!"}')
    agent_dir = _scaffold_default_agent(tmp_path)

    initial = runner.invoke(app, ["run", str(agent_dir), '{"text":"hi"}', "--mock"])
    assert initial.exit_code == 0
    run_id = _read_latest_run_id(tmp_path)

    result = runner.invoke(
        app,
        ["run", str(agent_dir), "--replay", run_id, "--mock", "-o", "text"],
    )
    assert result.exit_code == 0, result.stdout
    # Stderr carries the Rich summary; stdout is a JSON object with input + outputs.
    assert "agent replay" in result.stderr
    payload = json.loads(result.stdout)
    assert "input" in payload
    assert "recorded_output" in payload
    assert "current_output" in payload
