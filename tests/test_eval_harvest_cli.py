"""CLI — ``mdk eval harvest <agent>`` / ``mdk eval-harvest`` (ADR 016 D1).

Covers:

* ``mdk eval harvest <agent>`` produces proposals from local prod runs and
  writes them to the review file (``evals/harvested.jsonl``).
* **Proposed-not-applied:** without ``--accept`` the live ``evals/dataset.jsonl``
  is NOT created / modified; ``--accept`` appends to it.
* ``--source`` and ``--limit`` are honored; ``-o -`` prints to stdout.
* The ``mdk eval harvest`` spelling and the ``mdk eval-harvest`` sibling both
  reach the same command.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import (
    FeedbackRecord,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)

_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: rag-qa
version: 0.1.0
description: harvest CLI test agent
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input: ./schema/input.json
  output: ./schema/output.json
evals:
  dataset: ./evals/dataset.jsonl
"""


def _make_run(*, question: str = "hi") -> RunRecord:
    return RunRecord(
        run_id=f"run-{uuid4().hex[:12]}",
        job_id=f"job-{uuid4().hex[:12]}",
        tenant_id="local",  # CLI runtime persists under the "local" tenant
        agent="rag-qa",
        agent_version="0.1.0",
        prompt_hash="deadbeef",
        provider="openai/gpt-4o-mini",
        provider_version="2024-09",
        pricing_version="2024-09",
        status=JobStatus.SUCCESS,
        input={"question": question},
        output={"answer": "an answer"},
        metrics=Metrics(
            cost_usd=0.0001,
            latency_ms=100,
            tokens=TokenUsage(input=10, output=5),
            pricing_version="2024-09",
        ),
    )


def _feedback(*, run: RunRecord, score: int) -> FeedbackRecord:
    return FeedbackRecord(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        agent=run.agent,
        user_id="u1",
        score=score,
    )


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    """A minimal agent directory the harvest command can load."""
    d = tmp_path / "rag-qa"
    (d / "schema").mkdir(parents=True)
    (d / "agent.yaml").write_text(_AGENT_YAML)
    (d / "prompt.md").write_text("Answer: {{ input.question }}")
    (d / "schema" / "input.json").write_text(
        json.dumps({"type": "object", "properties": {"question": {"type": "string"}}})
    )
    (d / "schema" / "output.json").write_text(
        json.dumps({"type": "object", "properties": {"answer": {"type": "string"}}})
    )
    return d


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's sqlite at a tmp file and return its path."""
    db_path = tmp_path / "local.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    # Keep the harvest's MockProvider offline + deterministic.
    monkeypatch.setenv("MOVATE_TRACER", "silent")
    return db_path


async def _seed(records: list[RunRecord | FeedbackRecord]) -> None:
    storage = build_storage()
    await storage.init()
    try:
        for rec in records:
            if isinstance(rec, RunRecord):
                await storage.save_run(rec)
            else:
                await storage.save_feedback(rec)
    finally:
        await storage.close()


@pytest.mark.unit
def test_harvest_writes_review_file_not_dataset(agent_dir: Path, seeded_db: Path) -> None:
    down = _make_run(question="bad")
    asyncio.run(_seed([down, _feedback(run=down, score=-1)]))

    result = runner.invoke(
        app,
        ["eval", "harvest", str(agent_dir), "--source", "thumbs-down"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    review = agent_dir / "evals" / "harvested.jsonl"
    dataset = agent_dir / "evals" / "dataset.jsonl"
    assert review.exists()
    # Proposed-not-applied: the live dataset is untouched.
    assert not dataset.exists()

    rows = [json.loads(line) for line in review.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["input"] == down.input
    assert rows[0]["harvest"]["source_run_id"] == down.run_id
    assert "needs-review" in rows[0]["tags"]
    assert "expected" not in rows[0]  # thumbs-down asserts no expected


@pytest.mark.unit
def test_harvest_accept_appends_to_dataset(agent_dir: Path, seeded_db: Path) -> None:
    # Pre-existing hand-authored dataset row that must be preserved.
    evals = agent_dir / "evals"
    evals.mkdir()
    (evals / "dataset.jsonl").write_text(
        json.dumps({"input": {"question": "existing"}, "expected": {"answer": "x"}}) + "\n"
    )

    up = _make_run(question="good")
    asyncio.run(_seed([up, _feedback(run=up, score=1)]))

    result = runner.invoke(
        app,
        ["eval", "harvest", str(agent_dir), "--source", "thumbs-up", "--accept"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    dataset = evals / "dataset.jsonl"
    rows = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    assert len(rows) == 2  # original preserved + 1 appended
    assert rows[0]["input"] == {"question": "existing"}
    appended = rows[1]
    assert appended["input"] == up.input
    assert appended["expected"] == up.output  # thumbs-up golden case
    assert appended["harvest"]["source_run_id"] == up.run_id
    # No separate review file written on the accept path.
    assert not (evals / "harvested.jsonl").exists()


@pytest.mark.unit
def test_harvest_limit_honored(agent_dir: Path, seeded_db: Path) -> None:
    records: list = []
    for i in range(4):
        r = _make_run(question=f"q{i}")
        records += [r, _feedback(run=r, score=-1)]
    asyncio.run(_seed(records))

    result = runner.invoke(
        app,
        ["eval", "harvest", str(agent_dir), "--source", "thumbs-down", "--limit", "2"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    review = agent_dir / "evals" / "harvested.jsonl"
    rows = [line for line in review.read_text().splitlines() if line.strip()]
    assert len(rows) == 2


@pytest.mark.unit
def test_harvest_stdout_output(agent_dir: Path, seeded_db: Path) -> None:
    down = _make_run(question="bad")
    asyncio.run(_seed([down, _feedback(run=down, score=-1)]))

    # ``-o`` overlaps the ``eval`` command's own ``--output`` short flag, so
    # the ``mdk eval harvest`` spelling can't forward it — use the
    # collision-free ``mdk eval-harvest`` sibling for short-flag forms.
    result = runner.invoke(
        app,
        ["eval-harvest", str(agent_dir), "--source", "thumbs-down", "-o", "-"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The proposed case JSON is printed; the dataset/review file is not written.
    assert down.run_id in result.stdout
    assert not (agent_dir / "evals" / "harvested.jsonl").exists()
    assert not (agent_dir / "evals" / "dataset.jsonl").exists()


@pytest.mark.unit
def test_eval_harvest_sibling_spelling(agent_dir: Path, seeded_db: Path) -> None:
    """`mdk eval-harvest` reaches the same command as `mdk eval harvest`."""
    down = _make_run(question="bad")
    asyncio.run(_seed([down, _feedback(run=down, score=-1)]))

    result = runner.invoke(
        app,
        ["eval-harvest", str(agent_dir), "--source", "thumbs-down"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (agent_dir / "evals" / "harvested.jsonl").exists()


@pytest.mark.unit
def test_harvest_unknown_source_errors(agent_dir: Path, seeded_db: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "harvest", str(agent_dir), "--source", "bogus"],
    )
    assert result.exit_code == 2
