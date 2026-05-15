"""Sprint Q — `mdk replay <run-id>` CLI tests.

Distinct from the existing test_replay.py (which tests the underlying
replay engine). This file targets the CLI command at
:mod:`movate.cli.replay_cmd`.

Three layers:

1. **Helpers** — _resolve_agent_dir handles present/missing/non-dir.
2. **CLI miss paths** — unknown run-id, missing agent dir, both exit
   with the right code and a clear message.
3. **CLI happy path** — replay against a real recorded run uses
   MockProvider; --diff shows side-by-side; --json emits the new
   RunRecord. End-to-end through a temp SQLite DB + a real agent
   scaffolded from the bundled template.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.cli.replay_cmd import _outputs_equal, _resolve_agent_dir
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage import SqliteProvider

runner = CliRunner(mix_stderr=False)

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold_agent(dst: Path, name: str = "demo") -> Path:
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Project with a `demo` agent scaffolded under agents/."""
    (tmp_path / "movate.yaml").write_text("api_version: movate/v1\nkind: Project\nname: t\n")
    _scaffold_agent(tmp_path / "agents" / "demo", name="demo")
    return tmp_path


def _seed_run(
    db_path: Path,
    *,
    run_id: str = "r-test-1",
    agent: str = "demo",
    tenant_id: str = "local",
) -> RunRecord:
    """Persist one RunRecord and return it for downstream assertions."""
    rec = RunRecord(
        run_id=run_id,
        job_id="j-test-1",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="0.1.0",
        prompt_hash="h",
        provider="mock",
        provider_version="0",
        pricing_version="2026-05",
        status=JobStatus.SUCCESS,
        input={"text": "hello, replay"},
        output={"message": "echo: hello, replay"},
        metrics=Metrics(
            cost_usd=0.001,
            tokens=TokenUsage(input=10, output=5),
            provider="mock",
        ),
        created_at=datetime.now(UTC),
    )

    async def _save() -> None:
        provider = SqliteProvider(db_path=str(db_path))
        await provider.init()
        try:
            await provider.save_run(rec)
        finally:
            await provider.close()

    asyncio.run(_save())
    return rec


@pytest.fixture
def db_with_run(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, RunRecord]:
    """Populated SQLite DB with one RunRecord pointing at `demo`."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    rec = _seed_run(db_path)
    return db_path, rec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveAgentDir:
    def test_returns_path_when_present(self, project: Path) -> None:
        assert _resolve_agent_dir("demo", project) is not None

    def test_returns_none_when_missing(self, project: Path) -> None:
        assert _resolve_agent_dir("ghost", project) is None

    def test_returns_none_when_no_yaml(self, project: Path) -> None:
        """A folder under agents/ without agent.yaml shouldn't resolve."""
        (project / "agents" / "no_yaml").mkdir()
        assert _resolve_agent_dir("no_yaml", project) is None


@pytest.mark.unit
class TestOutputsEqual:
    def test_equal_dicts(self) -> None:
        assert _outputs_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})

    def test_unequal_dicts(self) -> None:
        assert not _outputs_equal({"a": 1}, {"a": 2})

    def test_none_treated_as_empty(self) -> None:
        assert _outputs_equal(None, {})
        assert _outputs_equal({}, None)


# ---------------------------------------------------------------------------
# CLI: miss paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_run_id_exits_1(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run not in storage → exit 1 + clear message."""
    monkeypatch.setenv("MOVATE_DB", str(tmp_path / "empty.db"))
    result = runner.invoke(app, ["replay", "no-such-run", "--mock", "--project-root", str(project)])
    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower() or "no-such-run" in combined


@pytest.mark.unit
def test_missing_agent_dir_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RunRecord exists but agent is gone from disk → exit 2."""
    # Project has no agents/ at all.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "movate.yaml").write_text("api_version: movate/v1\n")

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    _seed_run(db_path, agent="ghost-agent")

    result = runner.invoke(app, ["replay", "r-test-1", "--mock", "--project-root", str(proj)])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "ghost-agent" in combined or "not found" in combined.lower()


# ---------------------------------------------------------------------------
# CLI: happy path (real executor with MockProvider)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_replay_executes_with_mock(db_with_run: tuple[Path, RunRecord], project: Path) -> None:
    _db, original = db_with_run
    result = runner.invoke(
        app,
        ["replay", original.run_id, "--mock", "--project-root", str(project)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Summary table appears
    assert "Replay summary" in result.stdout
    # The replayed output panel shows up
    assert "Replayed output" in result.stdout


@pytest.mark.unit
def test_replay_diff_shows_both_outputs(db_with_run: tuple[Path, RunRecord], project: Path) -> None:
    _db, original = db_with_run
    result = runner.invoke(
        app,
        [
            "replay",
            original.run_id,
            "--diff",
            "--mock",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    # Both panels render
    assert "Original output" in result.stdout
    assert "Replayed output" in result.stdout
    # And either "match" or "differ" verdict appears
    combined = result.stdout.lower()
    assert "match" in combined or "differ" in combined


@pytest.mark.unit
def test_replay_json_output(db_with_run: tuple[Path, RunRecord], project: Path) -> None:
    _db, original = db_with_run
    result = runner.invoke(
        app,
        [
            "replay",
            original.run_id,
            "--mock",
            "--json",
            "--project-root",
            str(project),
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    # NEW RunRecord — distinct run_id from the original
    assert data["agent"] == "demo"
    assert data["run_id"] != original.run_id


@pytest.mark.unit
def test_replay_creates_new_run_record(db_with_run: tuple[Path, RunRecord], project: Path) -> None:
    """The original RunRecord must NOT be mutated; replay writes a new one."""
    db_path, original = db_with_run

    runner.invoke(
        app,
        ["replay", original.run_id, "--mock", "--project-root", str(project)],
    )

    async def _check() -> tuple[RunRecord | None, list[RunRecord]]:
        provider = SqliteProvider(db_path=str(db_path))
        await provider.init()
        try:
            fetched = await provider.get_run(original.run_id, tenant_id="local")
            all_runs = await provider.list_runs(agent="demo", tenant_id="local", limit=10)
            return fetched, all_runs
        finally:
            await provider.close()

    fetched, all_runs = asyncio.run(_check())
    # Original still there + unchanged
    assert fetched is not None
    assert fetched.output == original.output
    # Two records total: the original + the replay
    assert len(all_runs) == 2
