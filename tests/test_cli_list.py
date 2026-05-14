"""``mdk list`` — run + job discovery for ``mdk explain``.

Coverage:

* Default runs view returns most-recent rows
* ``--agent`` filter narrows by agent name
* ``--status`` filter narrows by status
* Invalid ``--status`` exits 2 with the valid set listed
* ``--jobs`` flips to JobRecord view
* ``--jobs --in-flight`` returns only QUEUED + RUNNING
* ``--limit`` caps row count
* ``--json`` emits parseable JSON
* ``--full-id`` shows complete UUIDs (for copy-paste to ``mdk explain``)
* Empty result set prints a friendly hint, not a crash
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
    JobRecord,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage.sqlite import SqliteProvider

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: str,
    agent: str = "faq-agent",
    status: JobStatus = JobStatus.SUCCESS,
    minutes_ago: int = 0,
    cost: float = 0.0023,
    latency_ms: int = 142,
) -> RunRecord:
    """Build a RunRecord with the fields the list command renders.

    Most defaults are picked so the rendered row looks "realistic" —
    a quick-cost, sub-second FAQ-agent success. Tests override the
    fields they care about.
    """
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
        input={"q": "hello"},
        output={"a": "world"} if status == JobStatus.SUCCESS else None,
        error=ErrorInfo(type="boom", message="boom") if status == JobStatus.ERROR else None,
        metrics=Metrics(
            latency_ms=latency_ms,
            tokens=TokenUsage(input=10, output=5),
            cost_usd=cost,
            provider="openai/gpt-4o-mini",
            pricing_version="2026.04",
        ),
        created_at=when,
    )


def _make_job(
    *,
    job_id: str,
    target: str = "faq-agent",
    status: JobStatus = JobStatus.QUEUED,
    minutes_ago: int = 0,
    attempt_count: int = 0,
) -> JobRecord:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return JobRecord(
        job_id=job_id,
        tenant_id="local",
        target=target,
        kind="agent",
        input={"q": "hi"},
        status=status,
        created_at=when,
        attempt_count=attempt_count,
    )


@pytest.fixture
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``build_storage`` at a temp SQLite so the list command's
    runtime sees an isolated, freshly-empty database.

    The CLI's ``build_local_runtime`` calls ``build_storage()`` which
    defaults to ``~/.movate/local.db``. We override the HOME env so
    that path lands under tmp_path instead, giving us a hermetic
    fixture per test.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _seed_storage(
    home: Path,
    *,
    runs: list[RunRecord] | None = None,
    jobs: list[JobRecord] | None = None,
) -> None:
    """Open the same SQLite path the CLI would use and seed records.

    Synchronous wrapper around an async core so the calling test
    function can stay sync — CliRunner.invoke calls ``asyncio.run``
    inside the command, and async test functions are themselves
    already inside an event loop, which collides. Keeping the test
    sync and running storage operations via ``asyncio.run`` here
    avoids that.
    """
    import asyncio as _asyncio  # noqa: PLC0415  -- local-only helper

    async def _core() -> None:
        db_path = home / ".movate" / "local.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        storage = SqliteProvider(db_path=str(db_path))
        await storage.init()
        try:
            for r in runs or []:
                await storage.save_run(r)
            for j in jobs or []:
                await storage.save_job(j)
        finally:
            await storage.close()

    _asyncio.run(_core())


# ---------------------------------------------------------------------------
# Default runs view
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_empty_storage_prints_friendly_hint(isolated_storage: Path) -> None:
    """No runs yet → the command prints a "no runs found" hint with
    the next command to try, not a stack trace."""
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no runs" in result.stdout.lower()
    assert "mdk run" in result.stdout


@pytest.mark.unit
def test_list_shows_recent_runs_newest_first(isolated_storage: Path) -> None:
    """Multiple runs render in newest-first order with truncated IDs."""
    _seed_storage(
        isolated_storage,
        runs=[
            _make_run(run_id="aaaaaaaa-1111-old", minutes_ago=30),
            _make_run(run_id="bbbbbbbb-2222-mid", minutes_ago=10),
            _make_run(run_id="cccccccc-3333-new", minutes_ago=1),
        ],
    )
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # All three appear (truncated to 8 chars).
    assert "aaaaaaaa" in result.stdout
    assert "bbbbbbbb" in result.stdout
    assert "cccccccc" in result.stdout
    # Newest first — "cccccccc" appears before "aaaaaaaa" in the rendered output.
    pos_new = result.stdout.index("cccccccc")
    pos_old = result.stdout.index("aaaaaaaa")
    assert pos_new < pos_old, "newest run should render first"


@pytest.mark.unit
def test_list_agent_filter_narrows_to_one(isolated_storage: Path) -> None:
    _seed_storage(
        isolated_storage,
        runs=[
            _make_run(run_id="aaa11111", agent="faq-agent"),
            _make_run(run_id="bbb22222", agent="sql-writer"),
        ],
    )
    result = runner.invoke(app, ["list", "--agent", "sql-writer"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "bbb22222" in result.stdout
    assert "aaa11111" not in result.stdout


@pytest.mark.unit
def test_list_status_filter_narrows_to_status(isolated_storage: Path) -> None:
    _seed_storage(
        isolated_storage,
        runs=[
            _make_run(run_id="aaa11111", status=JobStatus.SUCCESS),
            _make_run(run_id="bbb22222", status=JobStatus.ERROR),
        ],
    )
    result = runner.invoke(app, ["list", "--status", "error"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "bbb22222" in result.stdout
    assert "aaa11111" not in result.stdout


@pytest.mark.unit
def test_list_invalid_status_exits_two(isolated_storage: Path) -> None:
    """Typo in --status → clean error listing the valid options, not
    a confusing empty result set."""
    result = runner.invoke(app, ["list", "--status", "nope"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "invalid status" in combined.lower()


@pytest.mark.unit
def test_list_limit_caps_row_count(isolated_storage: Path) -> None:
    # Each ID must be distinguishable in its 8-char prefix so the
    # truncated-render test can count distinct rows. Use letters per index.
    ids = [f"{chr(ord('a') + i) * 8}-rest" for i in range(10)]  # aaaaaaaa-rest, bbbbbbbb-rest, ...
    _seed_storage(
        isolated_storage,
        runs=[_make_run(run_id=rid) for rid in ids],
    )
    result = runner.invoke(app, ["list", "--limit", "3"])
    assert result.exit_code == 0, result.stdout + result.stderr
    rendered = sum(1 for rid in ids if rid[:8] in result.stdout)
    assert rendered == 3, f"expected 3 rows, found {rendered}"


@pytest.mark.unit
def test_list_full_id_shows_complete_uuid(isolated_storage: Path) -> None:
    """``--full-id`` writes the whole UUID — useful for copy-paste
    into ``mdk explain``."""
    long_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _seed_storage(isolated_storage, runs=[_make_run(run_id=long_id)])
    result = runner.invoke(app, ["list", "--full-id"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert long_id in result.stdout


@pytest.mark.unit
def test_list_json_output_is_parseable(isolated_storage: Path) -> None:
    """``--json`` emits a JSON array the user can pipe to jq."""
    _seed_storage(
        isolated_storage,
        runs=[_make_run(run_id="aaaaaaaa", agent="faq", status=JobStatus.SUCCESS)],
    )
    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["run_id"] == "aaaaaaaa"
    assert entry["agent"] == "faq"
    assert entry["status"] == "success"
    assert "created_at" in entry
    assert "cost_usd" in entry


# ---------------------------------------------------------------------------
# Jobs view
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_jobs_empty_prints_hint(isolated_storage: Path) -> None:
    result = runner.invoke(app, ["list", "--jobs"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no jobs" in result.stdout.lower()
    assert "mdk submit" in result.stdout


@pytest.mark.unit
def test_list_jobs_in_flight_returns_queued_and_running_only(
    isolated_storage: Path,
) -> None:
    """``--jobs --in-flight`` filters out completed work.

    With a mix of QUEUED, RUNNING, SUCCESS, ERROR jobs in storage,
    only the first two should render — the operator just wants to
    know what's still active.
    """
    _seed_storage(
        isolated_storage,
        jobs=[
            _make_job(job_id="qu111111", status=JobStatus.QUEUED),
            _make_job(job_id="ru222222", status=JobStatus.RUNNING),
            _make_job(job_id="su333333", status=JobStatus.SUCCESS),
            _make_job(job_id="er444444", status=JobStatus.ERROR),
        ],
    )
    result = runner.invoke(app, ["list", "--jobs", "--in-flight"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "qu111111" in result.stdout
    assert "ru222222" in result.stdout
    assert "su333333" not in result.stdout
    assert "er444444" not in result.stdout


@pytest.mark.unit
def test_list_jobs_status_filter_works(isolated_storage: Path) -> None:
    _seed_storage(
        isolated_storage,
        jobs=[
            _make_job(job_id="qu111111", status=JobStatus.QUEUED),
            _make_job(job_id="su222222", status=JobStatus.SUCCESS),
        ],
    )
    result = runner.invoke(app, ["list", "--jobs", "--status", "queued"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "qu111111" in result.stdout
    assert "su222222" not in result.stdout
