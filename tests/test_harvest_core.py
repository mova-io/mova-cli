"""Core harvest transform + selection (ADR 016 D1).

Covers the pure logic in :mod:`movate.core.harvest`:

* selection by signal (thumbs-down / thumbs-up / low-score / sample),
  tenant-scoped;
* the transform: run input → case input, ``source_run_id`` provenance + the
  feedback signal recorded, thumbs-up → suggested ``expected``, thumbs-down /
  low-score → needs-review with NO asserted expected;
* serialization to a dataset row (the harvested-case JSONL shape).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from movate.core.harvest import (
    HarvestSource,
    harvest_runs,
    resolve_source,
    transform_run_to_case,
)
from movate.core.models import (
    FeedbackRecord,
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
)
from movate.testing import InMemoryStorage


def _make_run(
    *,
    tenant_id: str,
    agent: str = "rag-qa",
    run_id: str | None = None,
    question: str = "hi",
) -> RunRecord:
    return RunRecord(
        run_id=run_id or f"run-{uuid4().hex[:12]}",
        job_id=f"job-{uuid4().hex[:12]}",
        tenant_id=tenant_id,
        agent=agent,
        agent_version="1.0.0",
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


def _feedback(*, run: RunRecord, score: int, comment: str | None = None) -> FeedbackRecord:
    return FeedbackRecord(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        agent=run.agent,
        user_id="u1",
        score=score,
        comment=comment,
    )


# ---------------------------------------------------------------------------
# transform — the per-run rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_thumbs_up_suggests_expected_golden_case() -> None:
    run = _make_run(tenant_id="t1")
    fb = _feedback(run=run, score=1)
    case = transform_run_to_case(run, source=HarvestSource.THUMBS_UP, feedback=[fb])

    assert case.input == run.input
    assert case.expected == run.output  # prod output suggested as golden
    assert case.needs_review is False
    assert case.provenance["source_run_id"] == run.run_id
    assert case.provenance["source"] == "thumbs-up"
    assert case.provenance["feedback_score"] == 1


@pytest.mark.unit
def test_thumbs_down_needs_review_no_asserted_expected() -> None:
    run = _make_run(tenant_id="t1")
    fb = _feedback(run=run, score=-1, comment="wrong answer")
    case = transform_run_to_case(run, source=HarvestSource.THUMBS_DOWN, feedback=[fb])

    assert case.input == run.input
    assert case.expected is None  # never assert a known-bad output
    assert case.needs_review is True
    # the known-bad prod output is recorded in provenance for the reviewer
    assert case.provenance["prod_output"] == run.output
    assert case.provenance["source_run_id"] == run.run_id
    assert case.provenance["feedback_score"] == -1
    assert case.provenance["feedback_comment"] == "wrong answer"


@pytest.mark.unit
def test_low_score_needs_review() -> None:
    run = _make_run(tenant_id="t1")
    fb = _feedback(run=run, score=2)
    case = transform_run_to_case(run, source=HarvestSource.LOW_SCORE, feedback=[fb])
    assert case.expected is None
    assert case.needs_review is True


@pytest.mark.unit
def test_sample_needs_review_no_feedback() -> None:
    run = _make_run(tenant_id="t1")
    case = transform_run_to_case(run, source=HarvestSource.SAMPLE)
    assert case.expected is None
    assert case.needs_review is True
    assert "feedback_score" not in case.provenance


@pytest.mark.unit
def test_dataset_row_shape_round_trips() -> None:
    run = _make_run(tenant_id="t1")
    fb = _feedback(run=run, score=1)
    golden = transform_run_to_case(run, source=HarvestSource.THUMBS_UP, feedback=[fb])
    row = golden.to_dataset_row()
    assert row["input"] == run.input
    assert row["expected"] == run.output
    assert "harvested" in row["tags"]
    assert "needs-review" not in row["tags"]
    assert row["harvest"]["source_run_id"] == run.run_id

    needs = transform_run_to_case(run, source=HarvestSource.THUMBS_DOWN, feedback=[fb])
    needs_row = needs.to_dataset_row()
    assert "expected" not in needs_row  # no asserted expected on the row
    assert "needs-review" in needs_row["tags"]


@pytest.mark.unit
def test_resolve_source_rejects_unknown() -> None:
    assert resolve_source("thumbs-down") is HarvestSource.THUMBS_DOWN
    with pytest.raises(ValueError, match="unknown harvest source"):
        resolve_source("nonsense")


# ---------------------------------------------------------------------------
# harvest_runs — selection over storage, tenant-scoped
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.mark.unit
async def test_harvest_thumbs_down_selects_only_negative(
    storage: InMemoryStorage,
) -> None:
    up = _make_run(tenant_id="t1", question="good")
    down = _make_run(tenant_id="t1", question="bad")
    await storage.save_run(up)
    await storage.save_run(down)
    await storage.save_feedback(_feedback(run=up, score=1))
    await storage.save_feedback(_feedback(run=down, score=-1))

    result = await harvest_runs(
        storage, agent="rag-qa", tenant_id="t1", source=HarvestSource.THUMBS_DOWN
    )
    assert result.proposed_count == 1
    assert result.cases[0].provenance["source_run_id"] == down.run_id
    assert result.cases[0].needs_review is True


@pytest.mark.unit
async def test_harvest_low_score_includes_2_stars_and_thumbs_down(
    storage: InMemoryStorage,
) -> None:
    two = _make_run(tenant_id="t1", question="meh")
    five = _make_run(tenant_id="t1", question="great")
    down = _make_run(tenant_id="t1", question="bad")
    for r in (two, five, down):
        await storage.save_run(r)
    await storage.save_feedback(_feedback(run=two, score=2))
    await storage.save_feedback(_feedback(run=five, score=5))
    await storage.save_feedback(_feedback(run=down, score=-1))

    result = await harvest_runs(
        storage, agent="rag-qa", tenant_id="t1", source=HarvestSource.LOW_SCORE
    )
    harvested_ids = {c.provenance["source_run_id"] for c in result.cases}
    assert harvested_ids == {two.run_id, down.run_id}
    assert five.run_id not in harvested_ids


@pytest.mark.unit
async def test_harvest_sample_pulls_runs_without_feedback(
    storage: InMemoryStorage,
) -> None:
    r1 = _make_run(tenant_id="t1")
    r2 = _make_run(tenant_id="t1")
    await storage.save_run(r1)
    await storage.save_run(r2)
    # No feedback at all — sample is signal-agnostic.
    result = await harvest_runs(
        storage, agent="rag-qa", tenant_id="t1", source=HarvestSource.SAMPLE, limit=5
    )
    assert result.proposed_count == 2
    assert all(c.needs_review for c in result.cases)


@pytest.mark.unit
async def test_harvest_is_tenant_scoped(storage: InMemoryStorage) -> None:
    mine = _make_run(tenant_id="t1", question="mine")
    theirs = _make_run(tenant_id="t2", question="theirs")
    await storage.save_run(mine)
    await storage.save_run(theirs)
    await storage.save_feedback(_feedback(run=mine, score=-1))
    await storage.save_feedback(_feedback(run=theirs, score=-1))

    result = await harvest_runs(
        storage, agent="rag-qa", tenant_id="t1", source=HarvestSource.THUMBS_DOWN
    )
    ids = {c.provenance["source_run_id"] for c in result.cases}
    assert ids == {mine.run_id}  # never harvest another tenant's run


@pytest.mark.unit
async def test_harvest_limit_honored(storage: InMemoryStorage) -> None:
    for i in range(5):
        r = _make_run(tenant_id="t1", question=f"q{i}")
        await storage.save_run(r)
        await storage.save_feedback(_feedback(run=r, score=-1))
    result = await harvest_runs(
        storage,
        agent="rag-qa",
        tenant_id="t1",
        source=HarvestSource.THUMBS_DOWN,
        limit=2,
    )
    assert result.proposed_count == 2


@pytest.mark.unit
async def test_harvest_limit_zero_returns_nothing(storage: InMemoryStorage) -> None:
    r = _make_run(tenant_id="t1")
    await storage.save_run(r)
    await storage.save_feedback(_feedback(run=r, score=-1))
    result = await harvest_runs(
        storage,
        agent="rag-qa",
        tenant_id="t1",
        source=HarvestSource.THUMBS_DOWN,
        limit=0,
    )
    assert result.proposed_count == 0
