"""Shared Rich ``Tree`` renderer for a run's per-step execution breakdown.

ADR 024 D3: render a :class:`~movate.core.models.RunRecord` as a tree of LLM
*turns*, each parenting the *skill* / *retrieval* calls it dispatched, with
per-node cost / latency / tokens::

    run <short-id>  <status>  <total cost · latency>
    ├── turn 1  <model> · <in/out tokens> · <cost> · <latency>
    │   ├── retrieval.<skill>  <cost> · <latency>
    │   └── skill.<name>  ok|err · <cost> · <latency>
    └── turn 2  ...

This is the *offline-first* breakdown (the same rationale that already
justifies persisting ``skill_calls``): it reconstructs the per-step view
from the retained record alone — **no Langfuse / OTel backend required**.

Pure rendering, reused by BOTH ``mdk explain`` and ``mdk trace replay`` so
the tree never drifts between the two surfaces. Nothing here touches the
executor, models, or storage — PR 1 of ADR 024 did the retention; this is
PR 2 (rendering only).

Back-compat (ADR 024 D2): a legacy record with empty ``turns`` renders as a
**single synthesized node** built from the run-level ``Metrics`` — it never
crashes, and any orphan ``skill_calls`` (``turn == 0`` or pointing at a turn
that isn't retained) are grouped under that fallback node so nothing is
silently dropped.
"""

from __future__ import annotations

from rich.tree import Tree

from movate.core.models import JobStatus, RunRecord, SkillCallRecord, TurnRecord

__all__ = ["build_run_tree"]


# A skill call whose name starts with this prefix is a retrieval step
# (ADR 023 pre-retrieval or a model-driven KB lookup) and renders as a
# ``retrieval.*`` node rather than a generic ``skill.*`` node.
_RETRIEVAL_PREFIX = "retrieval"

# Length of the short run-id shown in the tree root (matches the 8-char id
# the `mdk run` post-run hint prints — see `cli/explain.py`).
_SHORT_ID_LEN = 8


def _status_label(status: str) -> str:
    """Short, colour-coded run-status label for the tree root."""
    if status == JobStatus.SUCCESS:
        return "[green]✓ success[/green]"
    if status == JobStatus.ERROR:
        return "[red]✗ error[/red]"
    if status == JobStatus.SAFETY_BLOCKED:
        return "[red]✗ safety_blocked[/red]"
    if status == JobStatus.DEAD_LETTER:
        return "[red]✗ dead_letter[/red]"
    if status == JobStatus.CANCELLED:
        return "[yellow]⊘ cancelled[/yellow]"
    return f"[yellow]{status}[/yellow]"


def _fmt_cost(cost_usd: float) -> str:
    return f"[green]${cost_usd:.6f}[/green]"


def _fmt_latency(latency_ms: float) -> str:
    return f"[cyan]{latency_ms:.0f} ms[/cyan]"


def _is_retrieval(skill: str) -> bool:
    """A retrieval node is named ``retrieval.*`` (ADR 023) or mentions ``kb``.

    The executor names pre-retrieval / model-driven KB lookups
    ``retrieval.<skill>``; older skill calls that hit the KB carry ``kb`` in
    their skill name (mirrors the inline-chunk heuristic in ``explain.py``).
    Either way they render as a retrieval node so the tree distinguishes
    "fetched context" from "called a tool".
    """
    lowered = skill.lower()
    return lowered.startswith(_RETRIEVAL_PREFIX) or "kb" in lowered


def _skill_node_name(call: SkillCallRecord) -> str:
    """Display name for a skill/retrieval node.

    Retrieval calls render as ``retrieval.<skill>`` (without duplicating the
    prefix when the skill name already carries it); generic tool calls render
    as ``skill.<name>``.
    """
    if _is_retrieval(call.skill):
        if call.skill.lower().startswith(_RETRIEVAL_PREFIX):
            return f"[magenta]{call.skill}[/magenta]"
        return f"[magenta]retrieval.{call.skill}[/magenta]"
    return f"[bold cyan]skill.{call.skill}[/bold cyan]"


def _skill_node_label(call: SkillCallRecord) -> str:
    """Label for one skill/retrieval child node: name · ok|err · cost · latency."""
    status = "[red]✗ err[/red]" if call.error else "[green]ok[/green]"
    parts = [status, _fmt_cost(call.cost_usd), _fmt_latency(call.latency_ms)]
    label = f"{_skill_node_name(call)}  " + " · ".join(parts)
    if call.error:
        label += f"  [dim red]{call.error[:60]}[/dim red]"
    return label


def _turn_node_label(turn: TurnRecord) -> str:
    """Label for one ``turn N`` parent node: model · tokens · cost · latency."""
    parts = [
        f"[bold]turn {turn.index}[/bold]",
        f"[dim]{turn.model}[/dim]" if turn.model else "[dim]—[/dim]",
        f"{turn.input_tokens} in → {turn.output_tokens} out",
        _fmt_cost(turn.cost_usd),
        _fmt_latency(turn.latency_ms),
    ]
    label = parts[0] + "  " + " · ".join(parts[1:])
    if turn.finish_reason:
        label += f"  [dim]({turn.finish_reason})[/dim]"
    return label


def _group_skill_calls(calls: list[SkillCallRecord]) -> dict[int, list[SkillCallRecord]]:
    """Group skill calls by the LLM ``turn`` index that dispatched them.

    Calls with ``turn == 0`` (legacy records / the pre-retrieval phase, ADR
    023 turn 0) bucket under key ``0`` so the caller can attach them to a
    pre-retrieval / fallback node rather than dropping them.
    """
    grouped: dict[int, list[SkillCallRecord]] = {}
    for call in calls:
        grouped.setdefault(call.turn, []).append(call)
    return grouped


def build_run_tree(record: RunRecord) -> Tree:
    """Build a Rich :class:`~rich.tree.Tree` of *record*'s execution breakdown.

    Reads the retained ``turns`` + ``skill_calls`` (ADR 024 D2). The root is
    the run id + status + run-level cost/latency; each ``turn`` is a child
    parenting its skill/retrieval calls (linked by ``SkillCallRecord.turn``
    == ``TurnRecord.index``).

    Legacy / single-node fallback (back-compat): when ``record.turns`` is
    empty, render one synthesized node from the run-level ``Metrics`` and hang
    any retained skill calls under it (so a tool-using legacy record still
    shows its steps). Never raises on a sparse / legacy record.
    """
    m = record.metrics
    short_id = (
        record.run_id[:_SHORT_ID_LEN] if len(record.run_id) > _SHORT_ID_LEN else record.run_id
    )
    root = Tree(
        f"[bold]run[/bold] [cyan]{short_id}[/cyan]  "
        f"{_status_label(record.status)}  "
        f"{_fmt_cost(m.cost_usd)} · {_fmt_latency(m.latency_ms)}"
    )

    skill_calls = list(record.skill_calls or [])
    grouped = _group_skill_calls(skill_calls)
    turns = list(record.turns or [])

    if not turns:
        # ---- Legacy / single-node fallback -------------------------------
        # No per-turn record retained (old record, or a single completion
        # predating the field). Synthesize one node from run-level metrics
        # and attach every skill call (regardless of `turn`) so nothing is
        # dropped — the tree degrades to "one turn + its tools".
        single = TurnRecord(
            index=1,
            model=m.provider or record.provider,
            input_tokens=m.tokens.input,
            output_tokens=m.tokens.output,
            cost_usd=m.cost_usd,
            latency_ms=m.latency_ms,
        )
        turn_node = root.add(_turn_node_label(single))
        for call in skill_calls:
            turn_node.add(_skill_node_label(call))
        return root

    # ---- Per-turn tree ---------------------------------------------------
    seen_turn_indexes = {t.index for t in turns}
    for turn in turns:
        turn_node = root.add(_turn_node_label(turn))
        for call in grouped.get(turn.index, []):
            turn_node.add(_skill_node_label(call))

    # Orphan skill calls: pre-retrieval (turn 0) or pointing at a turn index
    # that wasn't retained. Surface them under a synthetic node so the tree
    # never silently drops a captured step.
    orphans = [
        call
        for turn_idx, calls in grouped.items()
        if turn_idx not in seen_turn_indexes
        for call in calls
    ]
    if orphans:
        orphan_node = root.add("[dim]pre-retrieval / unattributed[/dim]")
        for call in orphans:
            orphan_node.add(_skill_node_label(call))

    return root
