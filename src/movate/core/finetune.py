"""Eval → fine-tune loop: dataset prep + the provider seam (ADR 063).

The self-improvement loop turns an agent's *graded* eval cases into a fine-tuned
model and proves it beats the base before anyone ships it. This module is the
deterministic, provider-agnostic core:

* :func:`examples_from_dataset_rows` — select the curated training set from the
  agent's ``evals/dataset.jsonl`` rows: golden cases (those with a reviewed
  ``expected``) and, when per-case eval scores are supplied, only cases at or
  above a score floor. Never raw, unvetted runs (anti-poisoning — the same
  discipline as the harvest pipeline).
* :func:`to_openai_jsonl` — render examples to a provider's fine-tune format
  (OpenAI chat JSONL today; other providers compose behind the same
  ``FineTuneExample`` shape).
* :class:`FineTuneProvider` — the adapter seam (ADR 063 D2). Hosted fine-tune is
  a provider call, so dispatch + poll live behind a Protocol; ``core`` depends
  on the Protocol, never a concrete SDK (CLAUDE.md rule 6/7).

The orchestration (async job + catalog registration + eval-vs-base gate) is the
worker/runtime half, built on this foundation in the ADR 063 rollout PRs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# Most hosted fine-tune APIs reject a training set below ~10 examples. We fail
# fast with a clear message rather than dispatch a job that the provider will
# reject minutes later (ADR 063 D1 — the dataset-size guard, rule 10).
MIN_DATASET_ROWS = 10


class FineTuneError(Exception):
    """Raised when a training set can't be built (too small / no usable cases)."""


@dataclass(frozen=True)
class FineTuneExample:
    """One supervised training pair: the user-facing prompt + the ideal answer.

    Provider-agnostic — :func:`to_openai_jsonl` (and future per-provider
    renderers) format a list of these into the wire schema.
    """

    prompt: str
    completion: str


def _as_text(value: Any) -> str:
    """Render an input/expected value to a content string.

    A single-field dict (the common ``{"question": "..."}`` / ``{"answer":
    "..."}`` shape) renders to that field's value; anything else serializes to
    compact JSON so the pair is faithful and round-trippable.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and len(value) == 1:
        only = next(iter(value.values()))
        return only if isinstance(only, str) else json.dumps(only, sort_keys=True)
    return json.dumps(value, sort_keys=True)


def examples_from_dataset_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    scores: Mapping[int, float] | None = None,
    min_score: float | None = None,
) -> list[FineTuneExample]:
    """Select the curated training set from ``evals/dataset.jsonl`` rows.

    A row contributes a training example only when it carries a reviewed
    ``expected`` (a *golden* case — the human-vetted ideal answer; raw
    unvetted runs never train, anti-poisoning). When ``scores`` (row-index →
    eval score) and ``min_score`` are both supplied, a golden row is
    additionally kept only if its score is at or above the floor — so an agent
    that already nails a case isn't over-weighted by a case it fails.

    Returns the examples in row order. Does not enforce the size floor — the
    caller does that (so a preview can report "would build N examples").
    """
    out: list[FineTuneExample] = []
    for i, row in enumerate(rows):
        expected = row.get("expected")
        if expected is None:
            continue  # not a golden case — never train on an unvetted output
        if min_score is not None and scores is not None:
            score = scores.get(i)
            if score is None or score < min_score:
                continue
        out.append(
            FineTuneExample(prompt=_as_text(row.get("input")), completion=_as_text(expected))
        )
    return out


def to_openai_jsonl(examples: Sequence[FineTuneExample]) -> str:
    """Render examples to OpenAI's chat fine-tune JSONL (one object per line).

    ``{"messages": [{"role": "user", ...}, {"role": "assistant", ...}]}`` — the
    format the OpenAI / Together fine-tune endpoints accept.
    """
    lines = [
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": ex.prompt},
                    {"role": "assistant", "content": ex.completion},
                ]
            }
        )
        for ex in examples
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def build_finetune_dataset(
    rows: Sequence[Mapping[str, Any]],
    *,
    scores: Mapping[int, float] | None = None,
    min_score: float | None = None,
) -> tuple[str, int]:
    """Select + render a training set; return ``(jsonl, example_count)``.

    Raises :class:`FineTuneError` when fewer than :data:`MIN_DATASET_ROWS`
    usable examples survive — failing fast before a provider job is ever
    dispatched (ADR 063 D1).
    """
    examples = examples_from_dataset_rows(rows, scores=scores, min_score=min_score)
    if len(examples) < MIN_DATASET_ROWS:
        raise FineTuneError(
            f"only {len(examples)} usable training example(s) "
            f"(need ≥ {MIN_DATASET_ROWS}); add more golden/reviewed eval cases "
            f"or lower --min-score."
        )
    return to_openai_jsonl(examples), len(examples)


# ---------------------------------------------------------------------------
# Provider seam (ADR 063 D2) — hosted fine-tune dispatch behind a Protocol.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FineTuneJob:
    """The status of a hosted fine-tune job, normalized across providers.

    ``status`` ∈ ``queued`` | ``running`` | ``succeeded`` | ``failed``.
    ``model_id`` is the canonical mdk model id (e.g.
    ``openai/ft:gpt-4o-mini:tenant:abc``) — set only on ``succeeded``.
    """

    provider_job_id: str
    status: str
    model_id: str | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in ("succeeded", "failed")


class FineTuneProvider(Protocol):
    """Adapter for a hosted fine-tune backend (OpenAI / Together / Bedrock …).

    Dispatch + poll only — the loop (dataset prep, catalog registration,
    eval-vs-base) is provider-agnostic and lives above this seam. Uses the
    tenant's BYOK key, never Movate's (ADR 063 D2).
    """

    async def start_finetune(
        self, *, base_model: str, training_jsonl: str, suffix: str, api_key: str
    ) -> FineTuneJob:
        """Upload the dataset + start a fine-tune; return the queued job."""
        ...

    async def poll_finetune(self, *, provider_job_id: str, api_key: str) -> FineTuneJob:
        """Fetch the current status of a previously-started job."""
        ...


__all__ = [
    "MIN_DATASET_ROWS",
    "FineTuneError",
    "FineTuneExample",
    "FineTuneJob",
    "FineTuneProvider",
    "build_finetune_dataset",
    "examples_from_dataset_rows",
    "to_openai_jsonl",
]
