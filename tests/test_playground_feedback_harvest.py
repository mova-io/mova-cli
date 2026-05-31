"""Playground feedback → proposed eval case (ADR 016 D1).

Covers ``movate.playground.harvest_feedback`` — the playground edge of the
harvest pipeline that turns a 👍/👎 on a turn into a *proposed* eval case in
``<agent>/evals/harvested.jsonl``, the same review artifact ``mdk eval harvest``
writes. These are pure-logic tests: the module is Chainlit-free by design, so
the verify gate runs them green on a no-extras install (we assert that import
invariant explicitly).

Behavior under test:

* **👎** captures the turn (input / known-bad output / run-id) as a
  *needs-review* case with NO asserted ``expected`` (anti-poisoning), routed
  through the harvest pipeline's :class:`~movate.core.harvest.ProposedCase`.
* **👍** lands a *golden* case with the prod output suggested as ``expected``.
* A 👎 with a tester-supplied *expected-better* answer attaches it as the
  suggested expected but stays needs-review.
* **Graceful degrade** — a missing agent name, or an unwritable review path,
  returns ``None`` (the caller keeps recording feedback as today) and never
  raises.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import movate.playground.harvest_feedback as hf
from movate.playground.harvest_feedback import (
    build_proposed_case,
    harvest_feedback_turn,
    review_path_for,
)

pytestmark = pytest.mark.unit


def test_module_imports_without_chainlit() -> None:
    """The harvest-feedback module must not import chainlit (no-extras gate)."""
    spec = importlib.util.find_spec("movate.playground.harvest_feedback")
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert "import chainlit" not in source


def test_thumbs_down_builds_needs_review_case_without_expected() -> None:
    """👎 → needs-review proposed case; input/output/run-id captured; no expected."""
    case = build_proposed_case(
        value="down",
        run_id="run-abc",
        user_input="What is the capital of France?",
        output_text="Berlin.",
    )
    assert case.needs_review is True
    # Anti-poisoning: a known-bad output is NEVER asserted as expected.
    assert case.expected is None
    assert case.input == {"message": "What is the capital of France?"}
    prov = case.provenance
    assert prov["source_run_id"] == "run-abc"
    assert prov["source"] == "thumbs-down"
    assert prov["feedback_score"] == -1
    assert prov["origin"] == "playground"
    # The known-bad output is preserved in provenance so a reviewer sees it.
    assert prov["prod_output"] == {"output": "Berlin."}


def test_thumbs_up_builds_golden_case_with_expected() -> None:
    """👍 → golden case; prod output suggested as expected; not needs-review."""
    case = build_proposed_case(
        value="up",
        run_id="run-xyz",
        user_input="2+2?",
        output_text="4",
    )
    assert case.needs_review is False
    assert case.expected == {"output": "4"}
    assert case.provenance["source"] == "thumbs-up"
    assert case.provenance["feedback_score"] == 1


def test_thumbs_down_with_expected_better_attaches_suggested_expected() -> None:
    """A tester's expected-better answer is suggested but case stays needs-review."""
    case = build_proposed_case(
        value="down",
        run_id="run-1",
        user_input="capital of France?",
        output_text="Berlin.",
        expected_better="Paris.",
        comment="wrong city",
    )
    assert case.needs_review is True
    assert case.expected == {"output": "Paris."}
    assert case.provenance["feedback_comment"] == "wrong city"


def test_harvest_writes_proposed_row_to_review_file(tmp_path: Path) -> None:
    """End to end: a 👎 turn appends one proposed row to harvested.jsonl."""
    written = harvest_feedback_turn(
        value="down",
        run_id="run-7",
        user_input="hello",
        output_text="bad answer",
        agent_name="rag-qa",
        root=tmp_path,
    )
    expected_path = review_path_for("rag-qa", root=tmp_path)
    assert written == expected_path
    assert expected_path.is_file()

    lines = expected_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["input"] == {"message": "hello"}
    # needs-review case → no asserted expected, tagged for the reviewer.
    assert "expected" not in row
    assert "harvested" in row["tags"]
    assert "needs-review" in row["tags"]
    assert row["harvest"]["source_run_id"] == "run-7"
    assert row["harvest"]["origin"] == "playground"


def test_harvest_appends_without_clobbering(tmp_path: Path) -> None:
    """A second harvested turn appends a second JSONL line."""
    harvest_feedback_turn(
        value="down",
        run_id="run-1",
        user_input="q1",
        output_text="a1",
        agent_name="agent",
        root=tmp_path,
    )
    harvest_feedback_turn(
        value="up",
        run_id="run-2",
        user_input="q2",
        output_text="a2",
        agent_name="agent",
        root=tmp_path,
    )
    path = review_path_for("agent", root=tmp_path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["expected"] == {"output": "a2"}


def test_graceful_degrade_missing_agent_name(tmp_path: Path) -> None:
    """No agent name → soft miss (None), no file written, no raise."""
    written = harvest_feedback_turn(
        value="down",
        run_id="run-9",
        user_input="x",
        output_text="y",
        agent_name=None,
        root=tmp_path,
    )
    assert written is None


def test_graceful_degrade_on_unwritable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O error during persist degrades to None — never raises."""

    def _boom(_path: Path, _row: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(hf, "append_review_row", _boom)
    written = harvest_feedback_turn(
        value="up",
        run_id="run-10",
        user_input="x",
        output_text="y",
        agent_name="agent",
        root=tmp_path,
    )
    assert written is None
