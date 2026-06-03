"""ADR 063 — fine-tune dataset prep + the provider seam (the deterministic core).

The orchestration (async job / catalog registration / eval-vs-base) is the
worker half; these pin the pure logic: which cases train, the size floor, the
wire format, and the FineTuneProvider seam shape.
"""

from __future__ import annotations

import json

import pytest

from movate.core.finetune import (
    MIN_DATASET_ROWS,
    FineTuneError,
    FineTuneExample,
    FineTuneJob,
    FineTuneProvider,
    build_finetune_dataset,
    examples_from_dataset_rows,
    to_openai_jsonl,
)


def _golden(i: int) -> dict:
    return {"input": {"question": f"q{i}"}, "expected": {"answer": f"a{i}"}, "tags": ["harvested"]}


def test_only_golden_cases_train() -> None:
    """A row trains ONLY if it has a reviewed ``expected`` — unvetted rows
    (input only) never contribute (anti-poisoning)."""
    rows = [_golden(0), {"input": {"question": "q1"}}, _golden(2)]  # middle has no expected
    examples = examples_from_dataset_rows(rows)
    assert len(examples) == 2
    assert all(isinstance(e, FineTuneExample) for e in examples)
    assert examples[0].prompt == "q0" and examples[0].completion == "a0"


def test_score_floor_filters_when_scores_supplied() -> None:
    """With per-case scores + a floor, only golden rows at/above the floor train."""
    rows = [_golden(0), _golden(1), _golden(2)]
    scores = {0: 0.9, 1: 0.4, 2: 0.8}
    kept = examples_from_dataset_rows(rows, scores=scores, min_score=0.7)
    assert {e.completion for e in kept} == {"a0", "a2"}  # a1 (0.4) dropped


def test_single_field_dict_renders_to_value_else_json() -> None:
    """A single-field dict renders to its value; a multi-field one to JSON."""
    rows = [
        {"input": {"q": "hi"}, "expected": {"a": "yo"}},
        {"input": {"x": 1, "y": 2}, "expected": {"ok": True}},
    ]
    ex = examples_from_dataset_rows(rows)
    assert ex[0].prompt == "hi" and ex[0].completion == "yo"
    assert json.loads(ex[1].prompt) == {"x": 1, "y": 2}  # multi-field → JSON


def test_openai_jsonl_shape() -> None:
    jsonl = to_openai_jsonl([FineTuneExample(prompt="hi", completion="yo")])
    obj = json.loads(jsonl.strip())
    assert obj["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]


def test_build_dataset_enforces_size_floor() -> None:
    """Fewer than MIN_DATASET_ROWS usable examples fails fast — before any
    provider job is dispatched."""
    with pytest.raises(FineTuneError) as ei:
        build_finetune_dataset([_golden(i) for i in range(MIN_DATASET_ROWS - 1)])
    assert str(MIN_DATASET_ROWS) in str(ei.value)


def test_build_dataset_happy_path() -> None:
    rows = [_golden(i) for i in range(MIN_DATASET_ROWS)]
    jsonl, count = build_finetune_dataset(rows)
    assert count == MIN_DATASET_ROWS
    assert len(jsonl.strip().splitlines()) == MIN_DATASET_ROWS


def test_finetune_job_terminal() -> None:
    assert FineTuneJob("j1", "succeeded", model_id="openai/ft:x").terminal
    assert FineTuneJob("j1", "failed", error="boom").terminal
    assert not FineTuneJob("j1", "running").terminal


def test_provider_seam_is_satisfiable() -> None:
    """A concrete impl satisfies the FineTuneProvider Protocol (structural)."""

    class _Fake:
        async def start_finetune(self, *, base_model, training_jsonl, suffix, api_key):
            return FineTuneJob("job-1", "queued")

        async def poll_finetune(self, *, provider_job_id, api_key):
            return FineTuneJob(provider_job_id, "succeeded", model_id="openai/ft:demo")

    provider: FineTuneProvider = _Fake()
    assert provider is not None
