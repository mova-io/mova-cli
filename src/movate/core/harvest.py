"""Harvest prod runs into *proposed* eval-dataset cases (ADR 016, Decision D1).

The first piece of the continuous-improvement loop: turn real production runs
and their feedback into **proposed** eval cases that a human reviews before
they land in an agent's ``evals/dataset.jsonl``. The test set grows from real
usage instead of hand-authored guesses.

The dominant safety property is **human-gated / proposed-not-applied**: a
harvest only *produces* candidate cases (returned as JSON / written to a review
file); it NEVER mutates the live dataset. An explicit, deliberate accept step
(CLI ``--accept`` or a follow-up call to the dataset-upload endpoint) is what
appends them. This prevents feedback-poisoning — noisy or adversarial
thumbs-down can't silently corrupt the eval set.

This module is pure transform + selection logic over the existing
:class:`~movate.core.models.RunRecord` / :class:`~movate.core.models.FeedbackRecord`
records read through the :class:`~movate.storage.base.StorageProvider`
Protocol. It holds no I/O of its own beyond awaiting the storage reads it is
handed, so both the CLI (``mdk eval harvest``) and the runtime API
(``POST /api/v1/agents/{name}/dataset/harvest``) compose it identically.

A *proposed case* is a superset of an eval dataset row (see
:func:`movate.core.eval._parse_dataset_path` for the canonical case shape): the
run's **input** becomes the case input, and a ``harvest`` provenance block
records ``source_run_id``, the feedback signal, and the known prod output. For
**thumbs-up** runs the prod output is suggested as the ``expected`` reference
(a golden case); for **thumbs-down / low-score** runs the case is marked
**needs-review** with NO asserted ``expected`` so a human supplies the correct
answer — asserting the known-bad output as expected would be exactly the
poisoning we're guarding against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from movate.core.models import FeedbackRecord, JobStatus, RunRecord
from movate.storage.base import StorageProvider

# The thumbs convention on ``FeedbackRecord.score`` (see its docstring):
# ``-1`` = 👎, ``+1`` = 👍. Star ratings use ``1..5``; ``1`` doubles as
# "thumbs up" so a positive signal is ``>= 1`` and a negative one is ``< 0``.
THUMBS_UP_MIN = 1
THUMBS_DOWN = -1

# Default boundary for the ``low-score`` source when the caller doesn't
# override it. Star ratings (1-5) at or below 2, OR a thumbs-down (-1),
# count as "low". Kept conservative so a harvest doesn't sweep in mediocre
# 3-star runs by default.
DEFAULT_LOW_SCORE_MAX = 2

# Cap how many candidate runs a single harvest considers, independent of the
# per-source ``limit``. A safety valve against an enormous run table; the
# caller's ``limit`` is the real control.
MAX_CANDIDATE_RUNS = 1000


class HarvestSource(StrEnum):
    """Which prod signal drives candidate selection.

    All sources are tenant-scoped and agent-scoped at selection time.
    """

    THUMBS_DOWN = "thumbs-down"
    """Runs with at least one 👎 (score ``-1``). Cases to *fix* — marked
    needs-review with no asserted expected."""

    THUMBS_UP = "thumbs-up"
    """Runs with at least one 👍 (score ``>= 1``). Golden/positive cases —
    the prod output is suggested as ``expected``."""

    LOW_SCORE = "low-score"
    """Runs whose feedback score is at or below ``low_score_max`` (default
    ``2`` ⇒ 1-2 stars, or a thumbs-down). Also needs-review."""

    SAMPLE = "sample"
    """A signal-agnostic sample of recent successful runs — coverage for the
    happy path that never drew feedback. Needs-review (no expected)."""


@dataclass(frozen=True)
class ProposedCase:
    """One *proposed* eval case derived from a prod run.

    A superset of an ``evals/dataset.jsonl`` row: ``input`` + (optionally)
    ``expected`` are the eval-case fields a reviewer keeps; ``provenance``
    is the harvest audit block a reviewer reads to decide. ``needs_review``
    is ``True`` whenever no trustworthy ``expected`` could be asserted (the
    thumbs-down / low-score / sample cases).
    """

    input: dict[str, Any]
    """The run's input — becomes the eval case input verbatim."""

    expected: dict[str, Any] | None
    """Suggested reference output. Populated ONLY for thumbs-up runs (the
    prod output, a golden case). ``None`` for needs-review cases so a human
    supplies the correct expected — we never assert a known-bad output."""

    needs_review: bool
    """``True`` when a human must supply / confirm the expected before this
    case is trustworthy. Always ``True`` except for thumbs-up golden cases."""

    provenance: dict[str, Any]
    """Audit block: ``source_run_id``, the ``source`` signal, the feedback
    ``score``/``comment`` (when any), and the known prod ``output``. Carried
    so a reviewer can trace a case back to the run that produced it."""

    def to_dataset_row(self) -> dict[str, Any]:
        """Serialize to a JSONL dataset row (one harvested-case line).

        The row is a valid eval case (``input`` always present; ``expected``
        present only for golden cases) plus a ``harvest`` provenance block
        and a ``tags`` entry so harvested cases are greppable in the dataset.
        :func:`movate.core.eval._parse_dataset_path` ignores unknown keys, so
        the extra ``harvest`` block survives a round-trip without breaking the
        eval engine.
        """
        row: dict[str, Any] = {"input": self.input}
        if self.expected is not None:
            row["expected"] = self.expected
        row["tags"] = ["harvested"] + (["needs-review"] if self.needs_review else [])
        row["harvest"] = dict(self.provenance)
        return row


@dataclass
class HarvestResult:
    """Outcome of a harvest: the proposed cases + a short summary.

    Carries everything a CLI message or an API response needs without either
    edge having to re-derive counts.
    """

    agent: str
    source: HarvestSource
    cases: list[ProposedCase] = field(default_factory=list)
    runs_considered: int = 0
    """How many candidate runs the selection looked at (before per-run
    dedupe / transform). Useful for the "scanned N, proposed M" message."""

    @property
    def proposed_count(self) -> int:
        return len(self.cases)

    @property
    def needs_review_count(self) -> int:
        return sum(1 for c in self.cases if c.needs_review)

    @property
    def golden_count(self) -> int:
        return sum(1 for c in self.cases if not c.needs_review)

    def to_rows(self) -> list[dict[str, Any]]:
        """All proposed cases as JSONL dataset rows."""
        return [c.to_dataset_row() for c in self.cases]


def _feedback_signal(feedback: list[FeedbackRecord]) -> tuple[int | None, str | None]:
    """Reduce a run's feedback rows to a single representative signal.

    Returns ``(score, comment)`` where ``score`` is the most decisive signal
    (prefer the lowest score so a single 👎 among 👍s surfaces the problem)
    and ``comment`` is the first non-empty comment found. ``(None, None)``
    when there's no feedback at all (the sample source).
    """
    if not feedback:
        return None, None
    score = min(f.score for f in feedback)
    comment = next((f.comment for f in feedback if f.comment), None)
    return score, comment


def transform_run_to_case(
    run: RunRecord,
    *,
    source: HarvestSource,
    feedback: list[FeedbackRecord] | None = None,
) -> ProposedCase:
    """Turn one prod run + its feedback into a :class:`ProposedCase`.

    Transform rules (D1):

    * The run's ``input`` is the case input verbatim.
    * **thumbs-up** ⇒ suggest the run's ``output`` as ``expected`` (golden
      case); ``needs_review=False``.
    * **thumbs-down / low-score / sample** ⇒ ``expected=None`` and
      ``needs_review=True`` — a human supplies the correct answer. The known
      prod output is recorded in provenance (so a reviewer sees what went
      wrong) but is NEVER asserted as expected.
    """
    feedback = feedback or []
    score, comment = _feedback_signal(feedback)

    provenance: dict[str, Any] = {
        "source_run_id": run.run_id,
        "source": source.value,
        "agent_version": run.agent_version,
        "prod_output": run.output,
    }
    if score is not None:
        provenance["feedback_score"] = score
    if comment:
        provenance["feedback_comment"] = comment

    is_golden = source is HarvestSource.THUMBS_UP
    return ProposedCase(
        input=run.input,
        expected=run.output if (is_golden and run.output is not None) else None,
        needs_review=not is_golden,
        provenance=provenance,
    )


def _run_matches_source(
    *,
    source: HarvestSource,
    feedback: list[FeedbackRecord],
    low_score_max: int,
) -> bool:
    """Does this run's feedback satisfy the requested source signal?

    ``sample`` matches regardless of feedback (it's signal-agnostic). The
    feedback-driven sources require at least one row meeting their bound.
    """
    if source is HarvestSource.SAMPLE:
        return True
    if not feedback:
        return False
    scores = [f.score for f in feedback]
    if source is HarvestSource.THUMBS_UP:
        return any(s >= THUMBS_UP_MIN for s in scores)
    if source is HarvestSource.THUMBS_DOWN:
        return any(s == THUMBS_DOWN for s in scores)
    if source is HarvestSource.LOW_SCORE:
        return any(s <= low_score_max for s in scores)
    return False


async def harvest_runs(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str,
    source: HarvestSource,
    limit: int = 20,
    since: datetime | None = None,
    low_score_max: int = DEFAULT_LOW_SCORE_MAX,
) -> HarvestResult:
    """Select prod runs by ``source`` signal and transform them to proposed cases.

    Tenant-scoped throughout: every storage read passes ``tenant_id`` so a
    caller can only ever harvest its own runs/feedback. Read-only — this
    function never writes anything (the human gate lives a layer up).

    Selection strategy:

    * **sample** — pull the most recent successful runs for the agent
      (``list_runs``), no feedback lookup required.
    * **thumbs-up / thumbs-down / low-score** — pull the agent's feedback
      rows (``list_feedback``), group by ``run_id``, keep the runs whose
      feedback satisfies the signal, then fetch each run.

    ``limit`` caps the number of *proposed cases* returned. ``since`` filters
    out runs/feedback created before the cutoff. Selection is newest-first
    (both ``list_runs`` and ``list_feedback`` order created_at DESC).
    """
    result = HarvestResult(agent=agent, source=source)
    limit = max(0, int(limit))
    if limit == 0:
        return result

    if source is HarvestSource.SAMPLE:
        runs = await storage.list_runs(
            agent=agent,
            tenant_id=tenant_id,
            status=JobStatus.SUCCESS.value,
            limit=min(limit, MAX_CANDIDATE_RUNS),
        )
        runs = [r for r in runs if since is None or r.created_at >= since]
        result.runs_considered = len(runs)
        for run in runs[:limit]:
            result.cases.append(transform_run_to_case(run, source=source))
        return result

    # Feedback-driven sources: gather the agent's feedback, group by run.
    feedback_rows = await storage.list_feedback(
        agent=agent,
        tenant_id=tenant_id,
        limit=MAX_CANDIDATE_RUNS,
    )
    if since is not None:
        feedback_rows = [f for f in feedback_rows if f.created_at >= since]

    by_run: dict[str, list[FeedbackRecord]] = {}
    for fb in feedback_rows:
        by_run.setdefault(fb.run_id, []).append(fb)

    # Preserve newest-first order: list_feedback is created_at DESC, so the
    # first time we see a run_id is its most-recent feedback.
    ordered_run_ids: list[str] = []
    seen: set[str] = set()
    for fb in feedback_rows:
        if fb.run_id not in seen:
            seen.add(fb.run_id)
            ordered_run_ids.append(fb.run_id)

    for run_id in ordered_run_ids:
        if len(result.cases) >= limit:
            break
        fb_for_run = by_run[run_id]
        if not _run_matches_source(source=source, feedback=fb_for_run, low_score_max=low_score_max):
            continue
        result.runs_considered += 1
        fetched = await storage.get_run(run_id, tenant_id=tenant_id)
        if fetched is None:
            # Feedback references a run we can't read (deleted, or — defense
            # in depth — a cross-tenant row that get_run refuses to leak).
            # Skip; never harvest a run we can't fetch under this tenant.
            continue
        result.cases.append(transform_run_to_case(fetched, source=source, feedback=fb_for_run))

    return result


def resolve_source(value: str) -> HarvestSource:
    """Map a ``--source`` flag string to a :class:`HarvestSource`.

    Raises :class:`ValueError` with the valid set on an unknown value so the
    CLI / API can surface a clean error.
    """
    try:
        return HarvestSource(value)
    except ValueError as exc:
        valid = ", ".join(s.value for s in HarvestSource)
        raise ValueError(f"unknown harvest source {value!r}; choose one of: {valid}") from exc


__all__ = [
    "DEFAULT_LOW_SCORE_MAX",
    "HarvestResult",
    "HarvestSource",
    "ProposedCase",
    "harvest_runs",
    "resolve_source",
    "transform_run_to_case",
]
