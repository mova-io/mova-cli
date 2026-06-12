"""``mdk diagnose run`` — evidence-cited per-run post-mortem.

Hermetic: every collector and the LLM call are monkeypatched at module level
(:mod:`movate.cli.diagnose_run_cmd` keeps them as module functions for exactly
this). No live runtime, Temporal, Langfuse, or model calls. Covers: evidence
assembly (all present / partially absent), the ``--no-llm`` path, the
``--json`` shape, citation-enforcement prompt content, honest-absent
rendering, unknown run id (exit 2), the Temporal history summarizer, and the
calibration case from the live incident (terminal temporal_workflow_error +
3 attempts of one activity + 3 identical ledger rows ⇒ the digest must point
at the failing activity + retry storm, not a generic "the workflow failed").
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from typer.testing import CliRunner

from movate.cli import diagnose_run_cmd as mod
from movate.cli.main import app as cli_app

runner = CliRunner(mix_stderr=False)

RUN_ID = "1f3c9b2a-aaaa-bbbb-cccc-000000000001"
TRACE_ID = "tr-cal-0001"


# ---------------------------------------------------------------------------
# Synthetic evidence — modeled on the real 2026-06-11 incident: terminal fact
# status=error error_type=temporal_workflow_error, 3 attempts of one activity,
# 3 identical sim-ledger rows.
# ---------------------------------------------------------------------------


def _terminal_fact() -> dict[str, Any]:
    return {
        "fact_id": "f-terminal",
        "kind": "workflow_run",
        "source_id": RUN_ID,
        "trace_id": TRACE_ID,
        "workflow": "expense-approval",
        "agent": None,
        "node_id": None,
        "status": "error",
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.0123,
        "tokens_in": 900,
        "tokens_out": 120,
        "latency_ms": 45210,
        "governance_effect": None,
        "error_type": "temporal_workflow_error",
        "created_at": "2026-06-11T10:05:00Z",
        "attributes": {},
    }


def _node_fact(node_id: str, status: str, created_at: str) -> dict[str, Any]:
    return {
        "fact_id": f"f-{node_id}",
        "kind": "run",
        "source_id": f"run-{node_id}",
        "trace_id": TRACE_ID,
        "workflow": "expense-approval",
        "agent": node_id,
        "node_id": node_id,
        "status": status,
        "runtime": "temporal",
        "route": None,
        "cost_usd": 0.004,
        "tokens_in": 300,
        "tokens_out": 40,
        "latency_ms": 1800,
        "governance_effect": None,
        "error_type": "provider_error" if status == "error" else None,
        "created_at": created_at,
        "attributes": {"workflow_run_id": RUN_ID},
    }


def _facts_present() -> dict[str, Any]:
    return {
        "present": True,
        "detail": "1 workflow_run fact(s), 2 node run fact(s)",
        "terminal_fact": _terminal_fact(),
        "node_facts": [
            _node_fact("classify", "success", "2026-06-11T10:00:10Z"),
            _node_fact("post-erp", "error", "2026-06-11T10:04:50Z"),
        ],
    }


def _temporal_present() -> dict[str, Any]:
    return {
        "present": True,
        "detail": "42 event(s), close status failed",
        "event_count": 42,
        "workflow_status": "failed",
        "workflow_failure": {
            "message": "Activity task failed",
            "stack": "Traceback ...",
            "cause": "ProviderError: upstream 503",
        },
        "activities": [
            {
                "activity_id": "post-erp",
                "activity_type": "run_agent_node",
                "scheduled_at": "2026-06-11T10:01:00Z",
                "attempts": 3,
                "failures": ["ProviderError: upstream 503", "ProviderError: upstream 503"],
                "outcome": "failed",
                "closed_at": "2026-06-11T10:04:55Z",
            }
        ],
        "timer_events": [],
        "signal_events": [],
        "started_at": "2026-06-11T10:00:00Z",
        "closed_at": "2026-06-11T10:05:00Z",
    }


def _ledger_present() -> dict[str, Any]:
    row = {
        "ts": 1781430000.0,
        "run_id": RUN_ID,
        "system": "erp",
        "action": "submit",
        "payload": {"document": "EXP-99", "amount": 4200},
    }
    return {
        "present": True,
        "detail": "3 side-effect row(s)",
        "rows": [dict(row) for _ in range(3)],
    }


def _spec_present() -> dict[str, Any]:
    return {
        "present": True,
        "detail": "3 node(s) from workflows/expense-approval/workflow.yaml",
        "path": "workflows/expense-approval/workflow.yaml",
        "runtime": "temporal",
        "entrypoint": "classify",
        "nodes": [
            {"id": "classify", "type": "decision", "default": "post-erp"},
            {"id": "post-erp", "type": "agent"},
            {"id": "finalize", "type": "agent"},
        ],
        "edges": [{"from": "post-erp", "to": "finalize"}],
    }


def _absent(detail: str) -> dict[str, Any]:
    return {"present": False, "detail": detail}


@pytest.fixture
def all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every collector returns synthetic evidence; API resolution is stubbed."""
    monkeypatch.setattr(mod, "_resolve_api", lambda target: ("dev", "https://api.test", "tok"))
    monkeypatch.setattr(mod, "_collect_facts", lambda b, t, r: _facts_present())
    monkeypatch.setattr(mod, "_collect_temporal_history", lambda r: _temporal_present())
    monkeypatch.setattr(
        mod,
        "_collect_langfuse",
        lambda tid: {
            "present": True,
            "detail": "2 observation(s)",
            "trace_url": f"https://langfuse.test/trace/{tid}",
            "observations": [
                {
                    "type": "GENERATION",
                    "name": "post-erp",
                    "model": "gpt-4o-mini",
                    "level": "ERROR",
                    "status_message": "upstream 503",
                    "input": "post this expense",
                    "output": None,
                    "tokens": {"input": 300, "output": 0, "total": 300},
                }
            ],
        },
    )
    monkeypatch.setattr(mod, "_collect_sim_ledger", lambda r: _ledger_present())
    monkeypatch.setattr(mod, "_collect_workflow_spec", lambda n: _spec_present())
    monkeypatch.setattr(
        mod,
        "_build_links",
        lambda r, t: {
            "temporal_ui": f"https://temporal.test/namespaces/default/workflows/{r}",
            "langfuse_trace": f"https://langfuse.test/trace/{t}",
        },
    )


def _llm_capture(monkeypatch: pytest.MonkeyPatch, reply: str | None = None) -> list[dict[str, str]]:
    """Patch the LLM seam; record (system, user, model) per call."""
    calls: list[dict[str, str]] = []
    payload = reply or json.dumps(
        {
            "summary": "post-erp retry-stormed [E12,E16]",
            "timeline": [{"at": "10:01", "what": "post-erp attempt 1 [E5]", "evidence": ["E5"]}],
            "divergence_point": "post-erp [E3,E5]",
            "probable_root_cause": "upstream 503 on post-erp's provider [E5,E12]",
            "confidence": "high",
            "missing_evidence": [],
        }
    )

    def fake(system_prompt: str, user_prompt: str, *, model: str) -> str:
        calls.append({"system": system_prompt, "user": user_prompt, "model": model})
        return payload

    monkeypatch.setattr(mod, "_run_llm", fake)
    return calls


# ---------------------------------------------------------------------------
# Evidence assembly + rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvidenceReport:
    def test_all_present_no_llm(self, monkeypatch: pytest.MonkeyPatch, all_present: None) -> None:
        calls = _llm_capture(monkeypatch)
        result = runner.invoke(cli_app, ["diagnose", RUN_ID, "--no-llm"])
        assert result.exit_code == 0, result.stdout + result.stderr
        # All five sources rendered present.
        assert result.stdout.count("✓") >= 5
        assert "✗" not in result.stdout
        # The deterministic signals name the failing activity + retry storm.
        assert "retry storm" in result.stdout
        assert "post-erp" in result.stdout
        # --no-llm really skips the model.
        assert calls == []
        assert "--no-llm" in result.stdout + result.stderr  # the skip note

    def test_partially_absent_rendered_honestly(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        monkeypatch.setattr(
            mod,
            "_collect_temporal_history",
            lambda r: _absent("Temporal unreachable (timed out after 20s)"),
        )
        monkeypatch.setattr(
            mod,
            "_collect_langfuse",
            lambda tid: (
                _absent("LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY not set")
                | {"trace_url": "https://langfuse.test/trace/tr-cal-0001"}
            ),
        )
        result = runner.invoke(cli_app, ["diagnose", RUN_ID, "--no-llm"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "✗" in result.stdout
        assert "Temporal unreachable" in result.stdout
        assert "LANGFUSE_PUBLIC_KEY" in result.stdout
        # Facts still present — terminal status surfaces.
        assert "status=error" in result.stdout

    def test_langfuse_absent_still_cites_trace_url(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        """URL-only fallback: detail-fetch absent, dashboard URL still shown."""
        monkeypatch.setattr(
            mod,
            "_collect_langfuse",
            lambda tid: {
                "present": False,
                "detail": "langfuse extra not installed",
                "trace_url": f"https://langfuse.test/trace/{tid}",
            },
        )
        monkeypatch.setattr(
            mod, "_build_links", lambda r, t: {"temporal_ui": None, "langfuse_trace": None}
        )
        result = runner.invoke(cli_app, ["diagnose", RUN_ID, "--no-llm"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "https://langfuse.test/trace/tr-cal-0001" in result.stdout
        assert "detail-fetch absent" in result.stdout


# ---------------------------------------------------------------------------
# --json shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJsonOutput:
    def test_shape_no_llm(self, monkeypatch: pytest.MonkeyPatch, all_present: None) -> None:
        _llm_capture(monkeypatch)
        result = runner.invoke(cli_app, ["diagnose", RUN_ID, "--no-llm", "--json"])
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert set(payload) == {"workflow_run_id", "evidence", "diagnosis"}
        assert payload["workflow_run_id"] == RUN_ID
        assert payload["diagnosis"] is None  # --no-llm
        evidence = payload["evidence"]
        assert set(evidence["sources"]) == {
            "facts",
            "temporal_history",
            "langfuse",
            "sim_ledger",
            "workflow_spec",
        }
        # Items are sequentially cited E1..En.
        ids = [item["id"] for item in evidence["items"]]
        assert ids == [f"E{i}" for i in range(1, len(ids) + 1)]
        assert evidence["links"]["temporal_ui"].endswith(RUN_ID)

    def test_diagnosis_included_with_llm(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        _llm_capture(monkeypatch)
        result = runner.invoke(cli_app, ["diagnose", RUN_ID, "--json"])
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        diagnosis = payload["diagnosis"]
        assert diagnosis["confidence"] == "high"
        assert "[E12,E16]" in diagnosis["summary"]
        # Links are injected deterministically, never model-invented.
        assert diagnosis["links"]["langfuse_trace"].endswith(TRACE_ID)


# ---------------------------------------------------------------------------
# The LLM pass — prompt contract + digest content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMPass:
    def test_prompt_enforces_citations_and_honesty(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        calls = _llm_capture(monkeypatch)
        result = runner.invoke(cli_app, ["diagnose", RUN_ID])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert len(calls) == 1
        system = calls[0]["system"]
        assert "MUST cite the evidence ids" in system
        assert "insufficient evidence" in system
        assert "Never invent" in system
        assert '"missing_evidence"' in system

    def test_digest_contains_evidence_ids_and_signals(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        calls = _llm_capture(monkeypatch)
        runner.invoke(cli_app, ["diagnose", RUN_ID])
        user = calls[0]["user"]
        assert RUN_ID in user
        assert '"id": "E1"' in user
        # CALIBRATION (2026-06-11 live incident): the digest must already
        # point at the failing activity + retry storm + duplicated side-effect
        # so the diagnosis can't degrade to "the workflow failed".
        assert "temporal_workflow_error" in user
        assert "retry storm" in user
        assert "'run_agent_node' (id post-erp) made 3 attempts" in user
        assert "3 identical erp.submit ledger rows" in user

    def test_absent_sources_listed_in_prompt(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        monkeypatch.setattr(
            mod, "_collect_sim_ledger", lambda r: _absent("MOVATE_PG_URL/MOVATE_DB_URL not set")
        )
        calls = _llm_capture(monkeypatch)
        runner.invoke(cli_app, ["diagnose", RUN_ID])
        assert "Absent evidence sources" in calls[0]["user"]
        assert "MOVATE_PG_URL" in calls[0]["user"]

    def test_model_flag_threads_through(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        calls = _llm_capture(monkeypatch)
        runner.invoke(cli_app, ["diagnose", RUN_ID, "--model", "anthropic/claude-x"])
        assert calls[0]["model"] == "anthropic/claude-x"

    def test_llm_failure_degrades_to_evidence(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        def boom(system_prompt: str, user_prompt: str, *, model: str) -> str:
            raise RuntimeError("no API key")

        monkeypatch.setattr(mod, "_run_llm", boom)
        result = runner.invoke(cli_app, ["diagnose", RUN_ID])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "retry storm" in result.stdout  # evidence still rendered
        assert "diagnosis unavailable" in result.stderr

    def test_non_json_reply_degrades(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        _llm_capture(monkeypatch, reply="sorry, I cannot help with that")
        result = runner.invoke(cli_app, ["diagnose", RUN_ID])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "non-JSON" in result.stderr


# ---------------------------------------------------------------------------
# Unknown run id / nothing reachable
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFailureModes:
    def test_unknown_run_id_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        monkeypatch.setattr(
            mod,
            "_collect_facts",
            lambda b, t, r: {
                "present": True,
                "detail": "0 workflow_run fact(s), 0 node run fact(s)",
                "terminal_fact": None,
                "node_facts": [],
            },
        )
        monkeypatch.setattr(
            mod, "_collect_temporal_history", lambda r: _absent("no history for id")
        )
        result = runner.invoke(cli_app, ["diagnose", "00000000-0000-0000-0000-00000000dead"])
        assert result.exit_code == 2
        assert "unknown workflow run id" in result.stderr

    def test_no_sources_reachable_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        for fn in (
            "_collect_facts",
            "_collect_temporal_history",
            "_collect_langfuse",
            "_collect_sim_ledger",
            "_collect_workflow_spec",
        ):
            monkeypatch.setattr(mod, fn, lambda *a: _absent("down"))
        result = runner.invoke(cli_app, ["diagnose", RUN_ID])
        assert result.exit_code == 1
        assert "no evidence source is reachable" in result.stderr

    def test_existing_agent_diagnose_contract_untouched(
        self, monkeypatch: pytest.MonkeyPatch, all_present: None
    ) -> None:
        """A non-UUID positional still routes to the agent diagnoser."""
        touched: list[str] = []

        def collector_spy(*args: Any) -> dict[str, Any]:
            touched.append("collector")
            return _absent("spy")

        monkeypatch.setattr(mod, "_collect_facts", collector_spy)
        # No targets configured in the hermetic env -> the agent path errors
        # at target resolution (exit 2) WITHOUT touching the run collectors.
        monkeypatch.setenv("MOVATE_CONFIG_PATH", "/nonexistent/config.yaml")
        result = runner.invoke(cli_app, ["diagnose", "my-agent-name"])
        assert touched == []
        assert result.exit_code == 2
        assert "unknown workflow run id" not in result.stderr

    def test_help_documents_dual_dispatch(self) -> None:
        result = runner.invoke(cli_app, ["diagnose", "--help"])
        assert result.exit_code == 0
        # Rich renders help with ANSI codes + wraps at the terminal width
        # (narrower in CI) — strip both before substring assertions.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).replace("\n", "")
        # Only structural anchors: at narrow CI widths rich truncates the
        # docstring prose entirely, so prose-phrase asserts can never be
        # width-stable. Behavior is covered by the dispatch tests above.
        assert "Usage:" in plain
        assert "show" in plain


# ---------------------------------------------------------------------------
# The Temporal history summarizer (proto-JSON shaped events)
# ---------------------------------------------------------------------------


def _history_with_retry_storm() -> dict[str, Any]:
    """3 attempts of one activity, then workflow failure — the live incident."""
    events: list[dict[str, Any]] = [
        {
            "eventId": "1",
            "eventTime": "2026-06-11T10:00:00Z",
            "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED",
            "workflowExecutionStartedEventAttributes": {},
        },
        {
            "eventId": "5",
            "eventTime": "2026-06-11T10:01:00Z",
            "eventType": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
            "activityTaskScheduledEventAttributes": {
                "activityId": "post-erp",
                "activityType": {"name": "run_agent_node"},
            },
        },
    ]
    for attempt in (1, 2, 3):
        started: dict[str, Any] = {"scheduledEventId": "5", "attempt": attempt}
        if attempt > 1:
            started["lastFailure"] = {"message": "ProviderError: upstream 503"}
        events.append(
            {
                "eventId": str(5 + attempt),
                "eventTime": f"2026-06-11T10:0{attempt}:30Z",
                "eventType": "EVENT_TYPE_ACTIVITY_TASK_STARTED",
                "activityTaskStartedEventAttributes": started,
            }
        )
    events.append(
        {
            "eventId": "9",
            "eventTime": "2026-06-11T10:04:55Z",
            "eventType": "EVENT_TYPE_ACTIVITY_TASK_FAILED",
            "activityTaskFailedEventAttributes": {
                "scheduledEventId": "5",
                "failure": {"message": "ProviderError: upstream 503"},
            },
        }
    )
    events.append(
        {
            "eventId": "10",
            "eventTime": "2026-06-11T10:05:00Z",
            "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED",
            "workflowExecutionFailedEventAttributes": {
                "failure": {
                    "message": "Activity task failed",
                    "stackTrace": "  at node post-erp\n",
                    "cause": {"message": "ProviderError: upstream 503"},
                }
            },
        }
    )
    return {"events": events, "workflowId": RUN_ID}


@pytest.mark.unit
class TestHistorySummarizer:
    def test_retry_storm_extracted(self) -> None:
        summary = mod._summarize_history(_history_with_retry_storm())
        assert summary["workflow_status"] == "failed"
        assert summary["workflow_failure"]["message"] == "Activity task failed"
        assert summary["workflow_failure"]["cause"] == "ProviderError: upstream 503"
        (activity,) = summary["activities"]
        assert activity["activity_type"] == "run_agent_node"
        assert activity["activity_id"] == "post-erp"
        assert activity["attempts"] == 3
        assert activity["outcome"] == "failed"
        assert "ProviderError: upstream 503" in activity["failures"][-1]
        assert summary["started_at"] == "2026-06-11T10:00:00Z"
        assert summary["closed_at"] == "2026-06-11T10:05:00Z"

    def test_signals_point_at_activity_not_generic(self) -> None:
        """Calibration: signals must name the activity + storm + duplicates."""
        sources = {
            "facts": _facts_present(),
            "temporal_history": {
                "present": True,
                "detail": "x",
                **mod._summarize_history(_history_with_retry_storm()),
            },
            "langfuse": _absent("off"),
            "sim_ledger": _ledger_present(),
            "workflow_spec": _spec_present(),
        }
        signals = mod._derive_signals(sources)
        joined = "\n".join(signals)
        assert "error_type=temporal_workflow_error" in joined
        assert "retry storm: activity 'run_agent_node' (id post-erp) made 3 attempts" in joined
        assert "3 identical erp.submit ledger rows" in joined
        # Not just "the workflow failed": the storm line is present and specific.
        assert any("retry storm" in s and "post-erp" in s for s in signals)

    def test_timers_and_signals_flagged_as_hitl(self) -> None:
        history = {
            "events": [
                {
                    "eventId": "1",
                    "eventTime": "t0",
                    "eventType": "EVENT_TYPE_TIMER_STARTED",
                    "timerStartedEventAttributes": {"startToFireTimeout": "3600s"},
                },
                {
                    "eventId": "2",
                    "eventTime": "t1",
                    "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED",
                    "workflowExecutionSignaledEventAttributes": {"signalName": "decision"},
                },
                {
                    "eventId": "3",
                    "eventTime": "t2",
                    "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED",
                },
            ]
        }
        summary = mod._summarize_history(history)
        assert summary["workflow_status"] == "completed"
        assert summary["signal_events"] == [{"signal_name": "decision", "at": "t1"}]
        assert len(summary["timer_events"]) == 1
        sources = {
            "facts": _absent("down"),
            "temporal_history": {"present": True, "detail": "x", **summary},
            "langfuse": _absent("off"),
            "sim_ledger": _absent("off"),
            "workflow_spec": _absent("off"),
        }
        joined = "\n".join(mod._derive_signals(sources))
        assert "HITL" in joined
