"""``mdk explain <run-id>`` — operator-facing run summarizer (Phase J-2).

Coverage:
* Exact-UUID lookup → renders the full panel
* 8-char-prefix lookup → resolves to the run + renders
* Ambiguous prefix → exits 2 with a disambiguation hint
* Unknown id (no match, no prefix hit) → exits 1 with the "try mdk list" hint
* --raw emits parseable JSON
* Error path renders the typed error block + optional hint
* Workflow context (workflow_run_id + node_id) renders when set
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import (
    ErrorInfo,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage.sqlite import SqliteProvider

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_cli_list.py's pattern)
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: str,
    agent: str = "faq-agent",
    status: JobStatus = JobStatus.SUCCESS,
    minutes_ago: int = 0,
    cost: float = 0.0023,
    latency_ms: int = 142,
    error: ErrorInfo | None = None,
    workflow_run_id: str | None = None,
    node_id: str | None = None,
) -> RunRecord:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return RunRecord(
        run_id=run_id,
        job_id=f"job-{run_id}",
        tenant_id="local",
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="abc123",
        provider="openai/gpt-4o-mini",
        provider_version="2024-07-18",
        pricing_version="2026.04",
        status=status,
        input={"q": "what is movate?"},
        output={"a": "a platform"} if status == JobStatus.SUCCESS else None,
        error=error,
        metrics=Metrics(
            latency_ms=latency_ms,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=cost,
            provider="openai/gpt-4o-mini",
            pricing_version="2026.04",
        ),
        created_at=when,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )


@pytest.fixture
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's SQLite at a temp home so each test has a fresh DB."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _seed_storage(home: Path, *, runs: list[RunRecord]) -> None:
    """Sync wrapper around async storage seeding — same pattern as
    tests/test_cli_list.py to dodge the event-loop collision when an
    async test function calls a Typer CLI that calls asyncio.run."""
    import asyncio as _asyncio  # noqa: PLC0415

    async def _core() -> None:
        db_path = home / ".movate" / "local.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        storage = SqliteProvider(db_path=str(db_path))
        await storage.init()
        try:
            for r in runs:
                await storage.save_run(r)
        finally:
            await storage.close()

    _asyncio.run(_core())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_exact_uuid_renders_full_panel(isolated_storage: Path) -> None:
    """Full UUID matches → panel renders header + metrics + input + output."""
    rid = "aaaaaaaa-1111-2222-3333-444444444444"
    _seed_storage(isolated_storage, runs=[_make_run(run_id=rid)])
    result = runner.invoke(app, ["explain", rid])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Header bits
    assert "faq-agent" in result.stdout
    assert "success" in result.stdout
    # Metrics
    assert "142 ms" in result.stdout
    # Input + output (the actual JSON content)
    assert "what is movate" in result.stdout
    assert "a platform" in result.stdout


@pytest.mark.unit
def test_explain_short_prefix_resolves_to_run(isolated_storage: Path) -> None:
    """8-char prefix is what `mdk list` shows by default — must resolve."""
    rid = "bbbbbbbb-1111-2222-3333-555555555555"
    _seed_storage(isolated_storage, runs=[_make_run(run_id=rid)])
    result = runner.invoke(app, ["explain", "bbbbbbbb"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "faq-agent" in result.stdout


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_unknown_id_exits_one_with_hint(isolated_storage: Path) -> None:
    """No exact match + no prefix hit → exit 1, point at `mdk list`."""
    result = runner.invoke(app, ["explain", "deadbeef-no-such-run"])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "no run found" in combined.lower()
    assert "mdk list" in combined


@pytest.mark.unit
def test_explain_ambiguous_prefix_exits_two(isolated_storage: Path) -> None:
    """Two runs share the prefix → exit 2 with disambiguation hint."""
    _seed_storage(
        isolated_storage,
        runs=[
            _make_run(run_id="ccccccc1-1111-aaaa"),
            _make_run(run_id="ccccccc2-2222-bbbb"),
        ],
    )
    result = runner.invoke(app, ["explain", "ccccccc"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "ambiguous" in combined.lower()
    assert "matched 2" in combined.lower() or "2 runs" in combined.lower()


@pytest.mark.unit
def test_explain_short_prefix_under_min_returns_not_found(
    isolated_storage: Path,
) -> None:
    """A 3-char input is below the prefix-match threshold; falls into
    not-found rather than ambiguous-prefix. Keeps the UX consistent —
    operators get either a match or a clear `not found` message."""
    _seed_storage(isolated_storage, runs=[_make_run(run_id="aaa11111-foo")])
    result = runner.invoke(app, ["explain", "aaa"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_raw_emits_parseable_json(isolated_storage: Path) -> None:
    """--raw is for piping — emit the RunRecord as JSON (no Rich frills)."""
    rid = "dddddddd-1111-2222-3333-666666666666"
    _seed_storage(isolated_storage, runs=[_make_run(run_id=rid, agent="sql-writer")])
    result = runner.invoke(app, ["explain", rid, "--raw"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_id"] == rid
    assert payload["agent"] == "sql-writer"
    assert payload["status"] == "success"


@pytest.mark.unit
def test_explain_full_id_shows_complete_uuid(isolated_storage: Path) -> None:
    """--full-id renders the full UUID in the header (default truncates)."""
    rid = "eeeeeeee-1111-2222-3333-777777777777"
    _seed_storage(isolated_storage, runs=[_make_run(run_id=rid)])
    result = runner.invoke(app, ["explain", rid, "--full-id"])
    assert result.exit_code == 0
    assert rid in result.stdout


# ---------------------------------------------------------------------------
# Failure-record rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_renders_error_block_when_run_failed(isolated_storage: Path) -> None:
    """Failed runs render a typed Error panel with category + message + hint."""
    rid = "ffffffff-1111-error-run-aaaa"
    err = ErrorInfo(
        type="schema_error",
        message="output failed schema: missing required field 'answer'",
        retryable=False,
        hint="check the agent's output.schema; the model may have produced an empty response",
    )
    _seed_storage(
        isolated_storage,
        runs=[_make_run(run_id=rid, status=JobStatus.ERROR, error=err)],
    )
    result = runner.invoke(app, ["explain", rid])
    assert result.exit_code == 0
    assert "schema_error" in result.stdout
    assert "missing required field" in result.stdout
    # Hint surfaced as actionable pointer
    assert "check the agent" in result.stdout.lower() or "hint" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Workflow context
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_renders_workflow_context_when_set(isolated_storage: Path) -> None:
    """A run that's part of a workflow renders the workflow_run_id +
    node_id so an operator can navigate up to the workflow view."""
    rid = "11111111-2222-3333-4444-555555555555"
    wf_id = "wf-abcdef"
    _seed_storage(
        isolated_storage,
        runs=[_make_run(run_id=rid, workflow_run_id=wf_id, node_id="classify")],
    )
    result = runner.invoke(app, ["explain", rid])
    assert result.exit_code == 0
    assert wf_id in result.stdout
    assert "classify" in result.stdout


# ---------------------------------------------------------------------------
# Footer / pointer to deeper trace surfaces
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_footer_points_at_trace_replay(isolated_storage: Path) -> None:
    """Footer surfaces `mdk trace replay` and Langfuse pointer — deeper
    timelines (guardrail / reflection / retry events) live there, not
    in RunRecord. Operator needs the breadcrumb."""
    rid = "22222222-3333-4444-5555-666666666666"
    _seed_storage(isolated_storage, runs=[_make_run(run_id=rid)])
    result = runner.invoke(app, ["explain", rid])
    assert result.exit_code == 0
    assert "trace replay" in result.stdout.lower()
    assert "langfuse" in result.stdout.lower()
