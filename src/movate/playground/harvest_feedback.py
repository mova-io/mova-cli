"""Playground feedback → *proposed* eval case (the harvest pipeline, online).

This is the playground edge of the continuous-improvement loop (ADR 016 D1).
``mdk eval harvest <agent>`` turns *batches* of prod runs + their feedback into
**proposed** eval cases written to ``<agent>/evals/harvested.jsonl`` for human
review. This module does the same thing **one turn at a time**, the moment a
tester clicks 👍/👎 in the playground — so interactive testing grows the
regression set as a side effect instead of needing a separate harvest pass.

The dominant safety property is identical to the batch path and to ADR 016:
**proposed-not-applied**. A thumbs-down lands as a *needs-review* case with NO
asserted ``expected`` (asserting the known-bad output would be exactly the
feedback-poisoning we guard against); a human supplies the correct answer before
it can enter the gate. A thumbs-up lands as a golden case with the prod output
suggested as ``expected``. Nothing here touches the live ``evals/dataset.jsonl``
— that promotion stays the deliberate ``mdk eval harvest --accept`` step.

Reuse, not reinvention: the proposed-eval-case *model* and its transform/
serialization come straight from :mod:`movate.core.harvest`
(:func:`~movate.core.harvest.transform_run_to_case` →
:meth:`~movate.core.harvest.ProposedCase.to_dataset_row`), and the artifact is
the same ``evals/harvested.jsonl`` review file the CLI writes. No new store.

Chainlit-free by design: this module never imports chainlit, so it is import-
safe on a no-extras install and unit-testable in isolation. The app's feedback
handler is the only caller; if anything here fails, the caller swallows it so
feedback still records exactly as before (graceful degrade — never error the
chat).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.harvest import ProposedCase

logger = logging.getLogger(__name__)

# Where the playground writes proposed cases. The SAME review file the
# ``mdk eval harvest`` CLI writes (its proposed-not-applied artifact), so the
# online and batch paths converge on one human-review surface per agent.
HARVEST_REVIEW_FILENAME = "harvested.jsonl"

# Opt-in override for the harvest root (the directory that holds per-agent
# folders). When unset we fall back to the current working directory — the
# standard project layout where ``mdk eval harvest <agent>`` also resolves
# ``<agent>/evals/harvested.jsonl`` relative to the project root.
HARVEST_ROOT_ENV_VAR = "MDK_PLAYGROUND_HARVEST_DIR"


def harvest_root(env: dict[str, str] | None = None) -> Path:
    """Resolve the directory that contains per-agent folders.

    ``MDK_PLAYGROUND_HARVEST_DIR`` wins when set (the launcher can point the
    playground at the project root explicitly); otherwise the current working
    directory, matching how ``mdk eval harvest`` resolves a bare agent name.
    """
    environ = env if env is not None else dict(os.environ)
    configured = (environ.get(HARVEST_ROOT_ENV_VAR) or "").strip()
    return Path(configured) if configured else Path.cwd()


def review_path_for(agent_name: str, *, root: Path | None = None) -> Path:
    """The ``<root>/<agent>/evals/harvested.jsonl`` review file for an agent."""
    base = root if root is not None else harvest_root()
    return base / agent_name / "evals" / HARVEST_REVIEW_FILENAME


def build_proposed_case(
    *,
    value: str,
    run_id: str,
    user_input: str,
    output_text: str,
    comment: str | None = None,
    expected_better: str | None = None,
    agent_version: str | None = None,
) -> ProposedCase:
    """Build a :class:`~movate.core.harvest.ProposedCase` from a feedback turn.

    Reuses the harvest pipeline's proposed-eval-case *model* directly and
    applies the SAME transform rules as
    :func:`~movate.core.harvest.transform_run_to_case` (ADR 016 D1):

    * **👍 (``value == "up"``)** → golden case; the prod output is suggested as
      ``expected``; ``needs_review=False``.
    * **👎 (anything else)** → ``needs_review=True`` with NO asserted
      ``expected`` (never assert a known-bad output — that's the poisoning we
      guard against). The known prod output is recorded in provenance so a
      reviewer sees what went wrong. If the tester supplied an
      ``expected_better`` answer we attach it as the reviewer's suggested
      expected, but the case stays ``needs_review`` until a human confirms it.

    We construct the case directly from the turn we already hold (input text +
    rendered output + run id) rather than fabricating a full ``RunRecord`` — the
    runtime already persisted the matching run + feedback row; this is the
    immediate, online proposal of the case.
    """
    from movate.core.harvest import ProposedCase  # noqa: PLC0415

    is_up = value == "up"
    score = 1 if is_up else -1
    run_input: dict[str, Any] = {"message": user_input}
    prod_output: dict[str, Any] = {"output": output_text}

    provenance: dict[str, Any] = {
        "source_run_id": run_id,
        "source": "thumbs-up" if is_up else "thumbs-down",
        # Marks the online (playground) origin so a reviewer can tell an
        # interactively-harvested case from a batch ``mdk eval harvest`` one.
        "origin": "playground",
        "agent_version": agent_version,
        "prod_output": prod_output,
        "feedback_score": score,
    }
    if comment:
        provenance["feedback_comment"] = comment

    expected: dict[str, Any] | None = None
    if is_up:
        expected = prod_output
    elif expected_better:
        # A reviewer-supplied better answer: attach it as the suggested
        # expected, but keep needs_review True until a human confirms.
        expected = {"output": expected_better}

    return ProposedCase(
        input=run_input,
        expected=expected,
        needs_review=not is_up,
        provenance=provenance,
    )


def append_review_row(path: Path, row: dict[str, Any]) -> None:
    """Append one proposed-case row to the review file (JSONL).

    Creates the ``evals/`` folder if needed and starts on a fresh line when an
    existing file lacks a trailing newline, so we never glue two rows together
    — mirroring the CLI's append behavior.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_leading_newline = (
        path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    )
    with path.open("a", encoding="utf-8") as fh:
        if needs_leading_newline:
            fh.write("\n")
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def harvest_feedback_turn(
    *,
    value: str,
    run_id: str,
    user_input: str,
    output_text: str,
    comment: str | None = None,
    expected_better: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """Persist a playground turn's feedback as a proposed eval case.

    Returns the review-file path on success, or ``None`` when the case could
    not be harvested (missing agent name, or any I/O / transform error). The
    caller treats ``None`` as a soft miss: feedback still records as today.
    Never raises — this is best-effort coverage growth, never a chat blocker.
    """
    if not agent_name or not run_id:
        return None
    try:
        case = build_proposed_case(
            value=value,
            run_id=run_id,
            user_input=user_input,
            output_text=output_text,
            comment=comment,
            expected_better=expected_better,
            agent_version=agent_version,
        )
        # ``to_dataset_row`` is the harvest pipeline's own serializer — the row
        # is exactly the shape ``mdk eval harvest`` writes (tags + provenance).
        row = case.to_dataset_row()
        path = review_path_for(agent_name, root=root)
        append_review_row(path, row)
        return path
    except Exception:  # best-effort; never break the chat
        logger.debug("playground feedback harvest skipped", exc_info=True)
        return None


__all__ = [
    "HARVEST_REVIEW_FILENAME",
    "HARVEST_ROOT_ENV_VAR",
    "append_review_row",
    "build_proposed_case",
    "harvest_feedback_turn",
    "harvest_root",
    "review_path_for",
]
