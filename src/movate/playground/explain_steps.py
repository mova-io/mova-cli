"""Pure transform: an ``explain`` decision-chain payload → render-ready Steps.

The Chainlit playground turns each completed turn into a "glass box": the
agent's internals (tool/skill calls, retrieved context, per-turn routing) are
shown inline as collapsible Chainlit ``cl.Step``s. This module is the *pure*
half of that feature — it maps the read-only ``GET /api/v1/runs/{id}/explain``
payload (the shared :func:`movate.core.explain.explain_run` shape, also emitted
by ``mdk explain --json``) into a flat list of :class:`ExplainStep` descriptors.

Kept Chainlit-free on purpose (mirroring the other ``playground`` pure modules):
the app's render loop turns each :class:`ExplainStep` into a ``cl.Step`` with no
business logic of its own, and the transform stays unit-testable on a no-extras
install. The transform NEVER raises on a malformed / partial payload — it skips
what it can't read and returns whatever it could build, so a glass-box render
can only ever *add* detail, never break the chat (playground rule: degrade,
don't error).

What the explain payload actually exposes today (and what we render):

* ``skill_calls`` — per tool/skill call: ``skill`` name, ``input``, ``output``
  or ``error``, ``latency_ms``, ``turn``. Rendered one Step each (kind
  ``"tool"``). A KB/retrieval skill whose output carries a ``chunks`` list is
  surfaced as a ``"retrieval"`` Step instead, with the chunks summarised.
* ``turns`` — per LLM round-trip: ``model``, token counts, ``cost_usd``,
  ``latency_ms``, ``finish_reason``. A turn with ``finish_reason == "tool_use"``
  is the closest thing the payload has to a *routing / branch decision* (the
  model chose to call tools vs. answer), so we emit a compact ``"decision"``
  Step for it. There is no explicit workflow branch-label field in the explain
  surface — see the module note below.

Note (honesty / future work): the explain payload has **no dedicated
"branch decision" or "route taken" field** for workflow agents — the decision
chain is reconstructed from ``turns`` + ``skill_calls``. We approximate routing
from ``finish_reason``; a richer per-node branch record would need an additive
field on the explain seam (out of scope for this playground-only change).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# How many characters of a JSON input/output preview to keep in a Step body.
_PREVIEW_CHARS = 2000
# How many retrieved chunks to list inside a retrieval Step.
_MAX_CHUNKS = 12
# How many characters of a single chunk's content to preview.
_CHUNK_CONTENT_CHARS = 240


@dataclass
class ExplainStep:
    """One render-ready node of the decision chain (Chainlit-agnostic).

    The app turns each into a collapsed ``cl.Step``: ``name`` is the Step
    title, ``kind`` drives the icon/label, and ``body`` is the markdown shown
    when expanded. ``children`` lets a turn parent the calls it dispatched.
    """

    name: str
    kind: str
    """One of ``"tool"``, ``"retrieval"``, ``"decision"`` — the semantic class
    of this node (used for the Step label/icon, not Chainlit's ``type``)."""
    body: str
    """Markdown body shown when the Step is expanded (default collapsed)."""
    children: list[ExplainStep] = field(default_factory=list)


def build_explain_steps(payload: dict[str, Any] | None) -> list[ExplainStep]:
    """Map an explain decision-chain *payload* into render-ready Steps.

    Returns an **empty list** when there is nothing to show — no payload, a
    payload with no ``skill_calls`` and no informative ``turns`` (a plain
    single-shot answer), or a payload we couldn't parse. The caller treats an
    empty list as "degrade to today's plain message", so the chat is never
    cluttered with an empty glass box and never errors on bad data.

    Structure: tool/retrieval Steps (one per ``skill_calls`` entry) are nested
    under the LLM turn that dispatched them when the linkage (``turn`` ==
    ``index``) is present; otherwise they render at the top level. Turns that
    only emitted a final answer (no tool use, no children) are omitted — they
    add no debugging signal beyond the final message, which is already shown.
    """
    if not isinstance(payload, dict):
        return []

    skill_calls = payload.get("skill_calls")
    turns = payload.get("turns")
    skill_calls = skill_calls if isinstance(skill_calls, list) else []
    turns = turns if isinstance(turns, list) else []

    # Build the per-call Steps first, grouped by the turn that dispatched them.
    by_turn: dict[int, list[ExplainStep]] = {}
    orphans: list[ExplainStep] = []
    for call in skill_calls:
        if not isinstance(call, dict):
            continue
        step = _step_from_call(call)
        if step is None:
            continue
        turn_idx = call.get("turn")
        if isinstance(turn_idx, int) and turn_idx > 0:
            by_turn.setdefault(turn_idx, []).append(step)
        else:
            orphans.append(step)

    out: list[ExplainStep] = []

    # When we have real per-turn records, nest calls under their turn and emit
    # a decision Step for turns that branched into tool use.
    rendered_turns: set[int] = set()
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        idx = turn.get("index")
        children = by_turn.get(idx, []) if isinstance(idx, int) else []
        decision = _decision_step(turn, children)
        if decision is not None:
            out.append(decision)
            if isinstance(idx, int):
                rendered_turns.add(idx)

    # Any tool/retrieval calls whose turn we did NOT render as a decision
    # (legacy records with no matching turn, or turn linkage absent) still
    # surface at the top level so nothing is silently dropped.
    for turn_idx, steps in by_turn.items():
        if turn_idx not in rendered_turns:
            out.extend(steps)
    out.extend(orphans)

    return out


def _step_from_call(call: dict[str, Any]) -> ExplainStep | None:
    """Build a tool/retrieval Step from one ``skill_calls`` entry."""
    skill = str(call.get("skill") or "skill")
    chunks = _retrieval_chunks(call)
    if chunks is not None:
        return ExplainStep(
            name=f"retrieval · {skill}",
            kind="retrieval",
            body=_retrieval_body(call, chunks),
        )
    return ExplainStep(name=f"tool · {skill}", kind="tool", body=_tool_body(call))


def _retrieval_chunks(call: dict[str, Any]) -> list[Any] | None:
    """Return the retrieved chunks for a KB/retrieval call, else ``None``.

    A call is "retrieval" when its output dict carries a non-empty ``chunks``
    list — the shape KB skills return. Robust to the output being absent /
    not-a-dict (errored or non-KB calls return ``None`` → rendered as a tool).
    """
    output = call.get("output")
    if not isinstance(output, dict):
        return None
    chunks = output.get("chunks")
    if isinstance(chunks, list) and chunks:
        return chunks
    return None


def _retrieval_body(call: dict[str, Any], chunks: list[Any]) -> str:
    """Markdown for a retrieval Step: the query + a numbered chunk list."""
    lines: list[str] = []
    query = _query_preview(call.get("input"))
    if query:
        lines.append(f"**Query:** {query}")
    lines.append(f"**Retrieved {len(chunks)} chunk(s):**")
    for i, chunk in enumerate(chunks[:_MAX_CHUNKS], 1):
        if not isinstance(chunk, dict):
            lines.append(f"{i}. {_truncate(str(chunk), _CHUNK_CONTENT_CHARS)}")
            continue
        score = chunk.get("score", chunk.get("similarity"))
        source = chunk.get("source") or chunk.get("chunk_id") or "—"
        source = str(source).rsplit("/", maxsplit=1)[-1]
        content = str(chunk.get("content", chunk.get("text", ""))).replace("\n", " ")
        score_str = f" `{float(score):.2f}`" if isinstance(score, int | float) else ""
        lines.append(f"{i}.{score_str} **{source}** — {_truncate(content, _CHUNK_CONTENT_CHARS)}")
    if len(chunks) > _MAX_CHUNKS:
        lines.append(f"_…and {len(chunks) - _MAX_CHUNKS} more chunk(s)._")
    lines.append(_latency_note(call))
    return "\n\n".join(p for p in lines if p)


def _tool_body(call: dict[str, Any]) -> str:
    """Markdown for a tool Step: input, output (or error), latency."""
    lines = [f"**Input**\n\n```json\n{_json_block(call.get('input'))}\n```"]
    error = call.get("error")
    if error:
        lines.append(f"**Error**\n\n```\n{_truncate(str(error), _PREVIEW_CHARS)}\n```")
    else:
        lines.append(f"**Output**\n\n```json\n{_json_block(call.get('output'))}\n```")
    lines.append(_latency_note(call))
    return "\n\n".join(p for p in lines if p)


def _decision_step(turn: dict[str, Any], children: list[ExplainStep]) -> ExplainStep | None:
    """Build a per-turn "decision" Step, or ``None`` when the turn is noise.

    A turn is worth showing when it either dispatched tool calls (it has
    ``children``) or explicitly branched into tool use
    (``finish_reason == "tool_use"``). A turn that just produced the final
    answer adds nothing beyond the message already on screen, so we drop it to
    keep the glass box focused.
    """
    finish = turn.get("finish_reason")
    branched = finish == "tool_use"
    if not children and not branched:
        return None

    idx = turn.get("index")
    label = f"decision · turn {idx}" if isinstance(idx, int) else "decision · turn"
    model = turn.get("model")
    bits: list[str] = []
    if model:
        bits.append(f"**Model:** {model}")
    if finish:
        verb = "called tools" if branched else f"finished (`{finish}`)"
        bits.append(f"**Decision:** {verb}")
    if children:
        names = ", ".join(c.name for c in children)
        bits.append(f"**Dispatched:** {names}")
    tok_in = turn.get("input_tokens")
    tok_out = turn.get("output_tokens")
    if isinstance(tok_in, int) or isinstance(tok_out, int):
        bits.append(f"**Tokens:** {tok_in or 0} in → {tok_out or 0} out")
    latency = turn.get("latency_ms")
    if isinstance(latency, int | float) and latency:
        bits.append(f"**Latency:** {int(latency)} ms")
    return ExplainStep(name=label, kind="decision", body="\n\n".join(bits), children=children)


# ---------------------------------------------------------------------------
# Small formatting helpers (all total / exception-free)
# ---------------------------------------------------------------------------


def _latency_note(call: dict[str, Any]) -> str:
    latency = call.get("latency_ms")
    return f"_{int(latency)} ms_" if isinstance(latency, int | float) and latency else ""


def _query_preview(data: Any) -> str:
    """Best-effort one-line query string from a KB call's input dict."""
    if isinstance(data, dict):
        for key in ("query", "question", "q", "text", "input"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return _truncate(val.strip(), 200)
    return ""


def _json_block(data: Any) -> str:
    """Pretty-printed, length-capped JSON for a fenced code block."""
    try:
        raw = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(data)
    return _truncate(raw, _PREVIEW_CHARS)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = ["ExplainStep", "build_explain_steps"]
