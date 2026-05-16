"""PR #104 — MockProvider returns dataset.jsonl[*].expected on each call.

Closes the demo-day annoyance where ``mdk eval --mock <agent>`` failed
EVERY case with ``schema_error: Additional properties are not allowed``
because the mock always returned the canned ``{"message": "mock"}``,
which doesn't conform to any non-trivial agent's output schema.

After this PR: when an agent ships an ``evals/dataset.jsonl`` (every
shipped template does), the mock cycles through the rows' ``expected``
outputs in order. Eval iterates the dataset in order, so per-call
matches per-case — all cases score 1.0 and the gate passes.

For single-shot ``mdk run --mock``, the mock returns the FIRST
dataset row's expected (still schema-conforming).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.providers.base import CompletionRequest, Message
from movate.providers.mock import MockProvider, load_dataset_expecteds

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# `load_dataset_expecteds` — best-effort dataset reader
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadDatasetExpecteds:
    def test_returns_expected_in_dataset_order(self, tmp_path: Path) -> None:
        dataset = tmp_path / "dataset.jsonl"
        dataset.write_text(
            json.dumps({"input": {"x": 1}, "expected": {"out": "first"}})
            + "\n"
            + json.dumps({"input": {"x": 2}, "expected": {"out": "second"}})
            + "\n"
        )
        result = load_dataset_expecteds(dataset)
        assert result == [{"out": "first"}, {"out": "second"}]

    def test_returns_empty_when_path_is_none(self) -> None:
        assert load_dataset_expecteds(None) == []

    def test_returns_empty_when_file_does_not_exist(self, tmp_path: Path) -> None:
        assert load_dataset_expecteds(tmp_path / "missing.jsonl") == []

    def test_skips_malformed_rows(self, tmp_path: Path) -> None:
        dataset = tmp_path / "dataset.jsonl"
        dataset.write_text(
            json.dumps({"input": {}, "expected": {"good": True}})
            + "\n"
            + "not valid json\n"
            + json.dumps({"input": {}, "expected": {"also_good": True}})
            + "\n"
        )
        result = load_dataset_expecteds(dataset)
        # Two valid rows, malformed line skipped silently.
        assert len(result) == 2
        assert {"good": True} in result
        assert {"also_good": True} in result

    def test_skips_rows_without_expected_key(self, tmp_path: Path) -> None:
        """An input-only row (no expected) isn't useful as a mock
        target — skip it rather than mocking an empty response."""
        dataset = tmp_path / "dataset.jsonl"
        dataset.write_text(
            json.dumps({"input": {"x": 1}})
            + "\n"
            + json.dumps({"input": {"x": 2}, "expected": {"y": 1}})
            + "\n"
        )
        result = load_dataset_expecteds(dataset)
        assert result == [{"y": 1}]


# ---------------------------------------------------------------------------
# MockProvider.configure_dataset
# ---------------------------------------------------------------------------


def _make_request(prompt: str) -> CompletionRequest:
    """Build a minimal CompletionRequest with one user message."""
    return CompletionRequest(
        provider="mock",
        messages=[Message(role="user", content=prompt)],
        params={},
    )


@pytest.mark.unit
class TestMockProviderDatasetMode:
    def test_default_response_when_no_dataset(self) -> None:
        """Unconfigured mock returns its canned response — regression
        for the existing single-shot test/mock surface."""
        mock = MockProvider()
        result = asyncio.run(mock.complete(_make_request("hello")))
        assert json.loads(result.text) == {"message": "mock response"}

    def test_cycles_through_dataset_in_order(self) -> None:
        """After configure_dataset, each complete() call returns the
        next entry in dataset order. Eval iterates rows in order so
        this matches case-by-case."""
        mock = MockProvider()
        expecteds = [
            {"answer": "first"},
            {"answer": "second"},
            {"answer": "third"},
        ]
        mock.configure_dataset(expecteds)
        results = [
            json.loads(asyncio.run(mock.complete(_make_request(f"q{i}"))).text) for i in range(3)
        ]
        assert results == expecteds

    def test_wraps_around_at_end(self) -> None:
        """If callers exceed the dataset length, the mock wraps —
        never raises IndexError. Reasonable for tools that loop /
        sample beyond the dataset (rare but possible)."""
        mock = MockProvider()
        mock.configure_dataset([{"a": 1}, {"b": 2}])
        results = [
            json.loads(asyncio.run(mock.complete(_make_request("q"))).text) for _ in range(5)
        ]
        assert results == [{"a": 1}, {"b": 2}, {"a": 1}, {"b": 2}, {"a": 1}]

    def test_judge_prompt_returns_judge_response_not_dataset(self) -> None:
        """LLM-as-judge prompts must keep returning the canned judge
        response — they're independent of the agent's dataset."""
        mock = MockProvider()
        mock.configure_dataset([{"actual_answer": "x"}])
        # Judge prompts are detected by the `Rubric:` substring.
        result = asyncio.run(mock.complete(_make_request("Rubric: score it on a scale of 0-1")))
        parsed = json.loads(result.text)
        assert "score" in parsed
        assert parsed["score"] == 0.5
        # The dataset entry was NOT consumed (judge bypasses the cycle).
        result2 = asyncio.run(mock.complete(_make_request("normal prompt")))
        assert json.loads(result2.text) == {"actual_answer": "x"}

    def test_reset_via_empty_list(self) -> None:
        """Passing an empty list to configure_dataset switches the
        mock back to default-response mode."""
        mock = MockProvider()
        mock.configure_dataset([{"x": 1}])
        result = asyncio.run(mock.complete(_make_request("first")))
        assert json.loads(result.text) == {"x": 1}
        # Reset.
        mock.configure_dataset([])
        result = asyncio.run(mock.complete(_make_request("after reset")))
        assert json.loads(result.text) == {"message": "mock response"}


# ---------------------------------------------------------------------------
# End-to-end: `mdk eval --mock` against a shipped template now PASSES
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mdk_eval_mock_passes_lead_qualifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signature demo annoyance pre-PR-#104: `mdk eval --mock` on
    lead-qualifier (or any agent with a non-trivial output schema)
    failed every case with `schema_error`. Post-PR, the mock returns
    each row's expected output and scoring passes."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.chdir(tmp_path / "p")
    runner.invoke(app, ["add", "lead-qualifier"], env={"COLUMNS": "200"})
    result = runner.invoke(
        app,
        ["eval", "lead-qualifier", "--mock", "--gate", "0.7"],
        env={"COLUMNS": "200"},
    )
    # The eval should PASS — every case scores 1.0 against its own
    # expected (since the mock returns exactly that).
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "overall_pass=true" in combined
    assert "verdict" in combined.lower()


@pytest.mark.unit
def test_mdk_eval_mock_passes_faq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same regression check for the faq template — simpler schema
    but ALSO failed pre-PR (mock returned {message: ...}, faq schema
    expects {answer: ..., confidence: ...})."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    result = runner.invoke(
        app,
        ["eval", "faq", "--mock", "--gate", "0.7"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "overall_pass=true" in result.stdout + result.stderr


@pytest.mark.unit
def test_mdk_run_mock_returns_dataset_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdk run --mock <agent>` with arbitrary input should return
    the FIRST dataset row's expected output (schema-conforming) —
    not the canned {message: ...}."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "p", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "p")
    runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    result = runner.invoke(
        app,
        [
            "run",
            "faq",
            "--mock",
            '{"question": "anything"}',
        ],
        env={"COLUMNS": "200"},
    )
    # Should succeed (status: success), not error with schema_error.
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    # status: error would mean we still hit the mismatch.
    assert "schema_error" not in combined
    # The faq template's dataset row 0 has `answer` + `confidence` —
    # output should now contain `answer` (from the dataset's expected).
    assert "answer" in combined
