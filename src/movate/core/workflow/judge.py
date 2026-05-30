"""Shared JUDGE-node execution (ADR 056 D2/D3).

A JUDGE workflow node runs an LLM judge over an artifact in workflow state and
produces the canonical verdict contract (ADR 056 D2):

    {"verdict": "accept" | "revise" | "parse_error",
     "score": float | null,
     "feedback": str,
     "terminate": bool}

``terminate`` is the *derived* field the backends gate on — exactly what the
Temporal compiler's ``_emit_judge_node`` expects (``if verdict['terminate']``).
It is computed here, in ONE place, so the native runner (D3) and the Temporal
activity (D5) arrive at the same answer for the same judge output: there is one
verdict shape and one ``terminate`` rule, no backend invents its own.

[bold]Execution-model reuse (ADR 056 D3).[/bold] The judge always runs through
the SAME :class:`movate.core.executor.Executor` every other node uses, so
tracing (ADR 024), metering (ADR 036), session, and BYOK (ADR 018) all flow
through the one place. There is **no second judge engine** — the parsing reuses
``movate.core.reflection.parse_verdict`` (ADR 017: adapt, don't reinvent).

Two judge forms (ADR 056 D1):

* ``judge_agent`` ref — load that agent bundle, run it through the Executor.
  Its output dict is expected to carry ``verdict`` / ``score`` / ``feedback``
  (a judge agent's output schema); we read those keys directly, falling back
  to parsing a textual field through ``parse_verdict`` if the agent emitted the
  verdict as a JSON string.
* inline ``criteria`` — no dedicated agent; we materialise a tiny ephemeral
  judge agent (reusing ``reflection.py``'s default judge prompt + the project's
  model defaults) once per ``(criteria)`` and run THAT through the Executor.
  Same single execution path; the only difference is where the prompt comes
  from.

Fail-open posture (mirrors ``reflection.py``): a judge that returns malformed
output yields ``verdict="parse_error"`` and ``terminate`` falls back to a safe
default rather than crashing the workflow. A flaky judge must not wedge a
bounded reflection loop — the loop's iteration cap (D4) bounds the blast radius.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from movate.core.reflection import JUDGE_PROMPT_TEMPLATE, parse_verdict

if TYPE_CHECKING:
    from movate.core.loader import AgentBundle

log = logging.getLogger(__name__)

# Default model for the inline-``criteria`` ephemeral judge (ADR 056 D1). A
# cheap, widely-available grader; project layered-defaults override it at load
# time. Authors wanting a specific judge model use the ``judge_agent`` form.
_DEFAULT_JUDGE_MODEL = "openai/gpt-4o-mini-2024-07-18"


@dataclass(frozen=True)
class JudgeOutcome:
    """The result of running one JUDGE node.

    ``state_value`` is the canonical D2 verdict dict stamped into workflow
    state under the judge node's id. ``terminate`` is surfaced separately so
    the caller can branch without re-reading the dict. ``response_data`` is the
    judge agent's raw ``RunResponse.data`` (for the runner's RunRecord view).
    """

    state_value: dict[str, Any]
    terminate: bool
    feedback: str


# Output-schema keys a judge agent is expected to populate. Kept permissive:
# any subset is fine (the parser fills gaps), and a judge that emits its verdict
# as a single JSON string in a ``verdict``/``raw`` field is also handled.
_VERDICT_KEYS = ("verdict", "score", "feedback")


def derive_terminate(
    *,
    verdict: str,
    score: float | None,
    pass_threshold: float | None,
) -> bool:
    """The ONE ``terminate`` rule shared by every backend (ADR 056 D2).

    * ``pass_threshold`` set ⇒ the eval-gate form: ``score >= threshold``
      terminates. A missing score with a threshold set is treated as a
      *non-terminating* fail (the artifact wasn't graded high enough to pass),
      so a bounded loop keeps trying rather than spuriously accepting.
    * ``pass_threshold`` unset ⇒ the categorical form: ``accept`` terminates.
    * ``parse_error`` is fail-open: with no threshold it soft-accepts
      (terminate) so a flaky judge never wedges a non-looping gate; with a
      threshold it cannot meet the bar (no score) so it does not terminate.
    """
    if pass_threshold is not None:
        return score is not None and score >= pass_threshold
    # No threshold: ``accept`` terminates; ``parse_error`` soft-accepts
    # (fail-open, matching reflection.py's posture) so the gate never
    # hard-blocks on a flaky judge; ``revise`` continues.
    return verdict in ("accept", "parse_error")


def verdict_from_response_data(data: dict[str, Any]) -> tuple[str, float | None, str]:
    """Extract ``(verdict, score, feedback)`` from a judge agent's output dict.

    Robust to two shapes a judge agent might emit:

    1. Structured — the output schema declares ``verdict`` / ``score`` /
       ``feedback`` keys directly. We read them (and clamp the score via the
       same coercion ``parse_verdict`` uses, by round-tripping through it when
       needed).
    2. Stringified — the agent emitted the verdict as a JSON string in a
       ``verdict`` / ``raw`` / ``output`` field. We parse that through
       ``reflection.parse_verdict`` (the canonical permissive parser).
    """
    raw_verdict = data.get("verdict")
    # Shape 1: a dict-native verdict key.
    if isinstance(raw_verdict, str) and raw_verdict in {"accept", "revise", "parse_error"}:
        score = data.get("score")
        # Reuse parse_verdict's coercion/clamping for the score by feeding it a
        # synthetic JSON object — keeps "one score rule" without exporting the
        # private coercion helper.
        parsed = parse_verdict(json.dumps({"verdict": raw_verdict, "score": score}))
        feedback = str(data.get("feedback") or parsed.feedback or "")
        return (raw_verdict, parsed.score, feedback)

    # Shape 2: a stringified verdict somewhere in the output.
    for key in ("verdict", "raw", "output", "text"):
        candidate = data.get(key)
        if isinstance(candidate, str) and candidate.strip():
            parsed = parse_verdict(candidate)
            if parsed.verdict != "parse_error":
                return (parsed.verdict, parsed.score, parsed.feedback)

    # Nothing verdict-like — parse_error (fail-open is the caller's job).
    log.warning("judge: no parseable verdict in agent output keys=%s", sorted(data))
    return ("parse_error", None, "")


def build_judge_state_value(
    *,
    verdict: str,
    score: float | None,
    feedback: str,
    terminate: bool,
) -> dict[str, Any]:
    """The canonical D2 verdict dict stamped into workflow state."""
    return {
        "verdict": verdict,
        "score": score,
        "feedback": feedback,
        "terminate": terminate,
    }


# ---------------------------------------------------------------------------
# Inline-criteria ephemeral judge agent (ADR 056 D1 — the ``criteria`` form).
# ---------------------------------------------------------------------------

# Cache materialised criteria-form judge agents by criteria hash so we write
# the tiny agent dir at most once per (process, criteria). Keyed on a sha256 of
# the criteria text; value is the resolved agent directory. Pure perf — the dir
# is deterministic, so a cache miss just re-writes identical bytes.
_CRITERIA_AGENT_DIRS: dict[str, Path] = {}


def criteria_judge_dir(criteria: str) -> Path:
    """Materialise (once) an ephemeral judge agent for an inline ``criteria``.

    The agent reuses ``reflection.py``'s default judge prompt (so the inline
    form grades exactly like the in-Executor reflection judge) and declares an
    output schema of ``{verdict, score?, feedback}``. Model is left to project
    defaults (the loader fills it from ``load_project_config().defaults`` like
    every other agent), so the criteria judge participates in the same model
    policy / BYOK path as any node.

    Returned directory is loadable by :func:`movate.core.loader.load_agent` and
    runnable through the Executor — keeping the criteria form on the single
    execution path (D3).
    """
    key = hashlib.sha256(criteria.encode("utf-8")).hexdigest()[:16]
    cached = _CRITERIA_AGENT_DIRS.get(key)
    if cached is not None and (cached / "agent.yaml").exists():
        return cached

    root = Path(tempfile.gettempdir()) / "mdk-judge-agents" / key
    (root / "schema").mkdir(parents=True, exist_ok=True)

    # Prompt: the default judge rubric template (reflection.py), with the
    # criteria as the rubric and the artifact injected via the agent input
    # namespace. ``JUDGE_PROMPT_TEMPLATE`` is a ``str.format`` template whose
    # literal JSON examples are escaped as ``{{...}}``; ``.format`` collapses
    # those to single braces (Jinja-safe — Jinja only treats ``{{``/``{%`` as
    # special). We format with a sentinel for the artifact, then swap in the
    # Jinja reference so the executor's renderer fills it from state.
    artifact_sentinel = "__MDK_JUDGE_ARTIFACT__"
    prompt_body = JUDGE_PROMPT_TEMPLATE.format(
        rubric=criteria.strip(), output=artifact_sentinel
    ).replace(artifact_sentinel, "{{ input.text }}")
    (root / "prompt.md").write_text(prompt_body)

    (root / "agent.yaml").write_text(
        json.dumps(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": f"judge-{key}",
                "version": "0.1.0",
                "description": "Ephemeral inline-criteria judge (ADR 056 D1).",
                # A cheap default model so the inline form is self-contained.
                # ``load_agent`` applies project layered-defaults on top, so a
                # project that pins a default model still wins; this is only the
                # floor for a judge declared with bare ``criteria`` and no
                # dedicated agent. Determinism for grading (temperature 0).
                "model": {
                    "provider": _DEFAULT_JUDGE_MODEL,
                    "params": {"temperature": 0.0, "max_tokens": 256},
                },
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
            }
        )
    )
    (root / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    (root / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "required": ["verdict"],
                "properties": {
                    "verdict": {"type": "string", "enum": ["accept", "revise"]},
                    "score": {"type": "number"},
                    "feedback": {"type": "string"},
                },
            }
        )
    )
    _CRITERIA_AGENT_DIRS[key] = root
    return root


def load_judge_bundle(
    *,
    judge_ref: str,
    criteria: str,
    defaults: Any = None,
) -> AgentBundle:
    """Load the judge agent bundle for either JUDGE form (ADR 056 D1).

    ``judge_ref`` (an absolute path the compiler resolved) wins; otherwise the
    inline ``criteria`` form materialises + loads an ephemeral judge agent. The
    spec validator guarantees exactly one is set, but we guard defensively.
    """
    from movate.core.loader import load_agent  # noqa: PLC0415

    if judge_ref:
        return load_agent(judge_ref, defaults=defaults)
    if criteria and criteria.strip():
        return load_agent(criteria_judge_dir(criteria), defaults=defaults)
    raise ValueError("judge node has neither a judge_agent ref nor inline criteria")
