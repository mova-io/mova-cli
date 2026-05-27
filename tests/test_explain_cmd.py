"""Tests for ``mdk explain``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.models import (
    ErrorInfo,
    JobStatus,
    Metrics,
    RunRecord,
    SkillCallRecord,
    TokenUsage,
    TurnRecord,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    run_id: str = "run-abc",
    agent: str = "faq-agent",
    agent_version: str = "0.1.0",
    status: str = JobStatus.SUCCESS,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    error: ErrorInfo | None = None,
    latency_ms: int = 42,
    cost_usd: float = 0.000019,
    tokens_in: int = 312,
    tokens_out: int = 87,
    tokens_cached: int = 0,
    provider: str = "openai/gpt-4o-mini-2024-07-18",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        job_id="job-1",
        tenant_id="local",
        agent=agent,
        agent_version=agent_version,
        prompt_hash="abc123",
        provider=provider,
        provider_version="1.0",
        pricing_version="2026",
        status=status,
        input=input or {"question": "What is the return policy?"},
        output=output,
        metrics=Metrics(
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            tokens=TokenUsage(input=tokens_in, output=tokens_out, cached_input=tokens_cached),
            provider=provider,
        ),
        error=error,
        created_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
    )


class _FakeStorage:
    """In-memory storage stub for explain tests (mirrors test_logs_cmd pattern)."""

    def __init__(self, records: list[RunRecord]) -> None:
        self._records = {r.run_id: r for r in records}
        self._list = records

    async def init(self) -> None:
        pass

    async def get_run(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        return self._records.get(run_id)

    async def list_runs(
        self,
        *,
        agent: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]:
        records = self._list
        if agent:
            records = [r for r in records if r.agent == agent]
        return records[:limit]


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_known_run_shows_decision_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <run-id> prints the run id, agent, input, LLM call, and output."""
    rec = _make_run(
        output={"answer": "Our return policy is 30 days...", "confidence": 0.9},
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-abc" in result.stdout
    assert "faq-agent" in result.stdout
    assert "0.1.0" in result.stdout
    # Input section
    assert "What is the return policy" in result.stdout
    # LLM call section
    assert "LLM call" in result.stdout
    assert "gpt-4o-mini" in result.stdout
    assert "312" in result.stdout  # tokens_in
    assert "87" in result.stdout  # tokens_out
    assert "42" in result.stdout  # latency_ms
    # Output section
    assert "return policy is 30 days" in result.stdout


@pytest.mark.unit
def test_explain_unknown_run_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <unknown-id> exits 1 with 'run not found'."""
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([]))

    result = runner.invoke(app, ["explain", "no-such-run"])

    assert result.exit_code == 1
    assert "not found" in result.stderr


@pytest.mark.unit
def test_explain_last_shows_most_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain --last shows the most-recent run without requiring a run ID."""
    rec = _make_run(
        run_id="run-xyz",
        agent="kb-agent",
        output={"answer": "yes"},
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "--last"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "run-xyz" in result.stdout
    assert "kb-agent" in result.stdout


@pytest.mark.unit
def test_explain_json_flag_emits_machine_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <id> --json emits valid JSON with the key fields."""
    rec = _make_run(output={"answer": "30 days"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["run_id"] == "run-abc"
    assert parsed["agent"] == "faq-agent"
    assert parsed["status"] == JobStatus.SUCCESS
    assert parsed["input"] == {"question": "What is the return policy?"}
    assert parsed["output"] == {"answer": "30 days"}
    assert "llm_call" in parsed
    assert parsed["llm_call"]["tokens_in"] == 312
    assert parsed["llm_call"]["tokens_out"] == 87
    assert parsed["llm_call"]["latency_ms"] == 42


@pytest.mark.unit
def test_explain_error_run_shows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain on a failed run shows the error type and message."""
    rec = _make_run(
        status=JobStatus.ERROR,
        output=None,
        error=ErrorInfo(type="provider_error", message="rate limit exceeded"),
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "provider_error" in result.stdout
    assert "rate limit exceeded" in result.stdout


@pytest.mark.unit
def test_explain_cost_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain shows the cost when it is non-zero."""
    rec = _make_run(cost_usd=0.000019, output={"answer": "yes"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "0.000019" in result.stdout


@pytest.mark.unit
def test_explain_cached_tokens_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain shows cached token count when non-zero."""
    rec = _make_run(tokens_cached=200, output={"answer": "cached"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "cached: 200" in result.stdout


@pytest.mark.unit
def test_explain_tracer_hint_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain always shows the MOVATE_TRACER hint for step-level tracing."""
    rec = _make_run(output={"answer": "yes"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "MOVATE_TRACER" in result.stdout


@pytest.mark.unit
def test_explain_success_status_icon(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain marks a successful run with a success indicator."""
    rec = _make_run(status=JobStatus.SUCCESS, output={"ok": True})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0
    assert "success" in result.stdout


@pytest.mark.unit
def test_explain_error_json_includes_error_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain --json for an error run includes the error dict."""
    rec = _make_run(
        status=JobStatus.ERROR,
        output=None,
        error=ErrorInfo(type="timeout", message="call timed out", retryable=True),
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["error"]["type"] == "timeout"
    assert parsed["error"]["message"] == "call timed out"
    assert parsed["output"] is None


# ---------------------------------------------------------------------------
# M6 milestone: --steps flag and SkillCallRecord tests
# ---------------------------------------------------------------------------


def _make_skill_calls() -> list[SkillCallRecord]:
    """Return a small list of sample SkillCallRecord objects for tests."""
    return [
        SkillCallRecord(
            step=1,
            skill="kb-vector-lookup",
            input={"query": "return policy"},
            output={"results": ["30 days", "no questions asked"]},
            latency_ms=123.4,
        ),
        SkillCallRecord(
            step=2,
            skill="calculator",
            input={"expression": "2 + 2"},
            output={"result": 4},
            latency_ms=5.0,
        ),
    ]


@pytest.mark.unit
def test_explain_steps_flag_shows_skill_calls_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <id> --steps renders 'Skill calls' heading and each skill name."""
    skill_calls = _make_skill_calls()
    rec = _make_run(output={"answer": "30 days"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Skill calls" in result.stdout
    assert "kb-vector-lookup" in result.stdout
    assert "calculator" in result.stdout


@pytest.mark.unit
def test_explain_without_steps_shows_hint_not_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --steps, a run with skill_calls shows a hint but not the full table."""
    skill_calls = _make_skill_calls()
    rec = _make_run(output={"answer": "30 days"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    # Hint line should mention the count
    assert "skill call(s)" in result.stdout
    # The detailed table columns should NOT appear without --steps
    assert "Skill calls" not in result.stdout
    assert "kb-vector-lookup" not in result.stdout


@pytest.mark.unit
def test_explain_json_with_steps_includes_skill_calls_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mdk explain <id> --json --steps includes a 'skill_calls' list in JSON output."""
    skill_calls = _make_skill_calls()
    rec = _make_run(output={"answer": "yes"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert "skill_calls" in parsed
    assert isinstance(parsed["skill_calls"], list)
    assert len(parsed["skill_calls"]) == 2
    first = parsed["skill_calls"][0]
    assert first["step"] == 1
    assert first["skill"] == "kb-vector-lookup"
    assert first["input"] == {"query": "return policy"}
    assert first["output"] == {"results": ["30 days", "no questions asked"]}
    assert first["latency_ms"] == pytest.approx(123.4)
    # skill_calls_hint must NOT appear when --steps is given
    assert "skill_calls_hint" not in parsed


@pytest.mark.unit
def test_explain_json_without_steps_includes_skill_calls_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mdk explain <id> --json without --steps includes 'skill_calls_hint', not the list."""
    skill_calls = _make_skill_calls()
    rec = _make_run(output={"answer": "yes"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert "skill_calls_hint" in parsed
    assert "2" in parsed["skill_calls_hint"]
    # Full skill_calls list must NOT appear without --steps
    assert "skill_calls" not in parsed or "skill_calls_hint" in parsed


@pytest.mark.unit
def test_explain_no_skill_calls_shows_no_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with skill_calls=[] shows no hint and no 'Skill calls' section."""
    rec = _make_run(output={"answer": "fine"})
    # _make_run leaves skill_calls as default empty list — no changes needed
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "skill call" not in result.stdout.lower()
    assert "Skill calls" not in result.stdout


@pytest.mark.unit
def test_explain_no_skill_calls_json_hint_is_no_skill_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With skill_calls=[], --json output hint says 'no skill calls'."""
    rec = _make_run(output={"answer": "fine"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert "skill_calls_hint" in parsed
    assert "no skill calls" in parsed["skill_calls_hint"].lower()


@pytest.mark.unit
def test_explain_steps_shows_step_numbers_and_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """--steps table includes step numbers and latency values for each call."""
    skill_calls = _make_skill_calls()
    rec = _make_run(output={"answer": "done"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    # step numbers
    assert "1" in result.stdout
    assert "2" in result.stdout
    # latency values (rendered as "<N> ms")
    assert "123" in result.stdout  # 123.4 ms truncated to 123 ms
    assert "5" in result.stdout  # 5.0 ms


@pytest.mark.unit
def test_explain_steps_shows_error_skill_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """--steps renders a failed skill call with its error text."""
    skill_calls = [
        SkillCallRecord(
            step=1,
            skill="external-lookup",
            input={"id": "42"},
            error="connection refused",
            latency_ms=15.0,
        )
    ]
    rec = _make_run(output={"answer": "partial"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "external-lookup" in result.stdout
    assert "connection refused" in result.stdout


# ---------------------------------------------------------------------------
# Part 2: --steps renders KB chunks inline
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_steps_renders_kb_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """--steps expands kb-vector-lookup output into a readable chunk table."""
    skill_calls = [
        SkillCallRecord(
            step=1,
            skill="kb-vector-lookup",
            input={"query": "refund policy"},
            output={
                "chunks": [
                    {
                        "content": "Customers may return items within 30 days.",
                        "score": 0.87,
                        "source": "docs/policy.pdf",
                    }
                ]
            },
            latency_ms=55.0,
        )
    ]
    rec = _make_run(output={"answer": "30 days"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    # The chunk table rule should appear with skill name and chunk count
    assert "kb-vector-lookup" in result.stdout
    assert "1 chunk(s) retrieved" in result.stdout
    # Score, source filename, and content preview should be present
    assert "0.87" in result.stdout
    assert "policy.pdf" in result.stdout
    assert "Customers may return items" in result.stdout


@pytest.mark.unit
def test_explain_steps_no_kb_chunks_no_extra_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-KB skill calls should not trigger any chunk table rendering."""
    skill_calls = [
        SkillCallRecord(
            step=1,
            skill="send-email",
            input={"to": "user@example.com", "subject": "hi"},
            output={"status": "sent"},
            latency_ms=30.0,
        )
    ]
    rec = _make_run(output={"answer": "email sent"})
    rec = RunRecord(**{**rec.model_dump(), "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    # send-email should appear in the skill table
    assert "send-email" in result.stdout
    # No chunk table should appear
    assert "chunk(s) retrieved" not in result.stdout


# ---------------------------------------------------------------------------
# #125: short-id PREFIX resolution — makes the `mdk run` 8-char hint
# (`mdk explain <run_short>`) actually resolve. Exact-id + --last unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_explain_resolves_unique_short_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """mdk explain <8-char-prefix> resolves to the one matching run.

    This is the exact UX the post-run hint relies on: `mdk run` prints
    `mdk explain <run_id[:8]>`, and that short prefix must resolve.
    """
    rec = _make_run(
        run_id="abcd1234-5678-90ab-cdef-1234567890ab",
        output={"answer": "30 days"},
    )
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    # The 8-char short id `mdk run` would print for this run.
    result = runner.invoke(app, ["explain", "abcd1234"])

    assert result.exit_code == 0, result.stdout + result.stderr
    # Resolved to the full run — header shows the full id + the output.
    assert "abcd1234-5678-90ab-cdef-1234567890ab" in result.stdout
    assert "return policy is 30 days" not in result.stdout  # sanity: this isn't that fixture
    assert "30 days" in result.stdout


@pytest.mark.unit
def test_explain_ambiguous_prefix_lists_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prefix matching >1 recent run errors helpfully and lists the ids."""
    rec_a = _make_run(run_id="abcd1111-aaaa", output={"answer": "a"})
    rec_b = _make_run(run_id="abcd2222-bbbb", output={"answer": "b"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec_a, rec_b]))

    # `abcd` is a prefix of BOTH run ids → ambiguous (no exact match exists).
    result = runner.invoke(app, ["explain", "abcd"])

    assert result.exit_code == 1
    assert "ambiguous" in result.stderr
    # Both candidate ids are listed so the user can pick a longer prefix.
    assert "abcd1111-aaaa" in result.stderr
    assert "abcd2222-bbbb" in result.stderr


@pytest.mark.unit
def test_explain_unknown_prefix_still_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prefix matching no recent run keeps the existing 'not found' path."""
    rec = _make_run(run_id="abcd1234-5678", output={"answer": "x"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "zzzz9999"])

    assert result.exit_code == 1
    assert "not found" in result.stderr


@pytest.mark.unit
def test_explain_exact_full_id_does_not_trigger_prefix_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: an exact full-id match resolves directly via get_run,
    even when other runs share its prefix — exact-match semantics unchanged."""
    exact = _make_run(run_id="abcd1234-5678-90ab", output={"answer": "exact"})
    sibling = _make_run(run_id="abcd1234-ffff-0000", output={"answer": "sibling"})

    class _CountingStorage(_FakeStorage):
        def __init__(self, records: list[RunRecord]) -> None:
            super().__init__(records)
            self.list_calls = 0

        async def list_runs(self, **kwargs: Any) -> list[RunRecord]:
            self.list_calls += 1
            return await super().list_runs(**kwargs)

    store = _CountingStorage([exact, sibling])
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: store)

    result = runner.invoke(app, ["explain", "abcd1234-5678-90ab"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "exact" in result.stdout
    # Exact match short-circuits before any prefix scan.
    assert store.list_calls == 0


# ---------------------------------------------------------------------------
# ADR 024 PR 2 (#101): per-step execution TREE under `--steps` + additive
# `turns` in `--json`. Tree = run → turn[i] → skill/retrieval children, with
# per-node cost/latency/tokens. Legacy records (no turns) degrade to a single
# node; the flat skill table and pre-existing `--json` keys stay intact.
# ---------------------------------------------------------------------------


def _make_turns() -> list[TurnRecord]:
    """Two LLM turns: turn 1 dispatches the skills, turn 2 is the final answer."""
    return [
        TurnRecord(
            index=1,
            model="openai/gpt-4o-mini-2024-07-18",
            input_tokens=200,
            output_tokens=40,
            cost_usd=0.000010,
            latency_ms=60,
            finish_reason="tool_use",
        ),
        TurnRecord(
            index=2,
            model="openai/gpt-4o-mini-2024-07-18",
            input_tokens=112,
            output_tokens=47,
            cost_usd=0.000009,
            latency_ms=82,
            finish_reason="final",
        ),
    ]


@pytest.mark.unit
def test_explain_steps_renders_turn_tree_with_skill_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--steps renders turns as parents and their skills/retrieval as children.

    A retrieval call and a tool call both made during turn 1 must nest under
    `turn 1`, each showing per-node cost/latency.
    """
    turns = _make_turns()
    skill_calls = [
        SkillCallRecord(
            step=1,
            skill="retrieval.kb-vector-lookup",
            input={"query": "return policy"},
            output={"chunks": []},
            latency_ms=12.0,
            cost_usd=0.0,
            turn=1,
        ),
        SkillCallRecord(
            step=2,
            skill="calculator",
            input={"expression": "2 + 2"},
            output={"result": 4},
            latency_ms=3.0,
            cost_usd=0.000020,
            turn=1,
        ),
    ]
    rec = _make_run(output={"answer": "30 days"})
    rec = RunRecord(**{**rec.model_dump(), "turns": turns, "skill_calls": skill_calls})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    # Tree header + turn parents
    assert "Execution tree" in out
    assert "turn 1" in out
    assert "turn 2" in out
    # Retrieval renders as a retrieval node; the tool call as a skill node.
    assert "retrieval.kb-vector-lookup" in out
    assert "skill.calculator" in out
    # Per-node cost/latency surfaced (turn 1 latency, skill latency).
    assert "60 ms" in out  # turn 1 latency
    assert "tool_use" in out  # turn 1 finish_reason
    # The flat skill-call table is STILL rendered beneath the tree.
    assert "Skill calls" in out


@pytest.mark.unit
def test_explain_steps_legacy_record_no_turns_renders_single_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy RunRecord with empty `turns` degrades to one node — no crash."""
    rec = _make_run(output={"answer": "ok"})  # turns defaults to []
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "Execution tree" in out
    # Single synthesized turn node from run-level metrics (one "turn 1").
    assert "turn 1" in out
    assert "turn 2" not in out
    # Run-level model + tokens carried into the fallback node.
    assert "gpt-4o-mini" in out


@pytest.mark.unit
def test_explain_steps_single_turn_no_skill_renders_one_turn_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-turn, no-skill run renders a one-turn tree (no children)."""
    turns = [
        TurnRecord(
            index=1,
            model="openai/gpt-4o-mini-2024-07-18",
            input_tokens=312,
            output_tokens=87,
            cost_usd=0.000019,
            latency_ms=42,
            finish_reason="final",
        )
    ]
    rec = _make_run(output={"answer": "yes"})
    rec = RunRecord(**{**rec.model_dump(), "turns": turns})  # no skill_calls
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--steps"])

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    assert "Execution tree" in out
    assert "turn 1" in out
    assert "turn 2" not in out
    # No skill/retrieval children, and no flat skill table for a no-skill run.
    assert "skill." not in out
    assert "Skill calls" not in out


@pytest.mark.unit
def test_explain_json_includes_turns_additively(monkeypatch: pytest.MonkeyPatch) -> None:
    """--json gains a `turns` array WITHOUT dropping or renaming any prior key."""
    turns = _make_turns()
    rec = _make_run(output={"answer": "30 days"})
    rec = RunRecord(**{**rec.model_dump(), "turns": turns})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    # New additive key.
    assert "turns" in parsed
    assert isinstance(parsed["turns"], list)
    assert len(parsed["turns"]) == 2
    assert parsed["turns"][0]["index"] == 1
    assert parsed["turns"][0]["finish_reason"] == "tool_use"
    assert parsed["turns"][0]["cost_usd"] == pytest.approx(0.000010)
    # ALL pre-existing keys retained, unchanged.
    assert parsed["run_id"] == "run-abc"
    assert parsed["agent"] == "faq-agent"
    assert parsed["status"] == JobStatus.SUCCESS
    assert parsed["input"] == {"question": "What is the return policy?"}
    assert parsed["output"] == {"answer": "30 days"}
    assert parsed["llm_call"]["tokens_in"] == 312
    assert parsed["llm_call"]["tokens_out"] == 87
    assert parsed["llm_call"]["latency_ms"] == 42
    assert "skill_calls_hint" in parsed  # default (no --steps) hint preserved


@pytest.mark.unit
def test_explain_json_legacy_record_emits_empty_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy record (no turns) emits `turns: []` so consumers read it safely."""
    rec = _make_run(output={"answer": "ok"})
    monkeypatch.setattr("movate.cli.explain.build_storage", lambda: _FakeStorage([rec]))

    result = runner.invoke(app, ["explain", "run-abc", "--json"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["turns"] == []
